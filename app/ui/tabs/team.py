"""Proposal Review > Team tab.

Manual roster entry with approval gate. Front-loads named personnel +
time allocations + labor categories BEFORE the Writer Team drafts. The
approved roster is injected into the writer's cached prefix so the
writer stops emitting [NEEDS_HUMAN] for staffing details.

Includes the two dialogs invoked from the tab:
  - `_open_team_member_dialog` — add / edit one team member
  - `_open_team_composer_dialog` — Sonnet-driven "Propose Team (AI)"
    preview + atomic apply
"""

from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from app.ui._shared import _empty_state

log = logging.getLogger(__name__)


_PERSON_KIND_LABEL = {
    "named": "Named",
    "tbh": "To-be-hired",
    "sub": "Subcontractor",
}
_PERSON_KIND_COLOR = {
    "named": "bg-emerald-50 border-emerald-300 text-emerald-800",
    "tbh": "bg-amber-50 border-amber-300 text-amber-800",
    "sub": "bg-sky-50 border-sky-300 text-sky-800",
}


def _open_team_member_dialog(
    proposal_id: int,
    member: dict | None,
    *,
    on_saved,
) -> None:
    """Add or edit dialog for a single team member. When `member` is
    None, opens in add mode (Save creates a new row). Otherwise edit
    mode (Save updates the existing row). on_saved fires after
    successful persist so the parent tab can refresh."""
    from app.services.team import (
        add_team_member,
        list_labor_categories,
        list_profile_person_names,
        list_role_names,
        update_team_member,
    )

    # {canonical_name: display_label} — label is "Name - Role" for
    # profile entries (so the user can disambiguate the two real
    # "Joshua" / "Justin" people who share first+last names) and
    # bare name for KB-only entries.
    profile_names = list_profile_person_names()
    # Searchable-but-editable suggestion lists for the role-name and
    # labor-category fields. Pulled from key_personnel + the canonical
    # pricing data; the user can still type a new value (with_input +
    # add-unique) for RFP-driven roles and categories not yet in the
    # profile.
    role_options = {r: r for r in list_role_names()}
    cat_options = {c: c for c in list_labor_categories()}
    # Sentinel option shown at the top of the dropdown so the user
    # has a one-click way to mark a role as TBH without having to
    # type the string AND remember to flip the kind radio.
    TBH_OPTION = "To Be Hired"

    is_edit = member is not None
    initial = member or {
        "role_name": "",
        "person_kind": "named",
        "assigned_person": "",
        "labor_category": "",
        "wage_band": "",
        "time_allocation_pct": 50,
        "experience_years": None,
        "phases_active": [],
        "bio_summary": "",
    }

    with ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-[95vw]"):
        ui.label("Edit Team Member" if is_edit else "Add Team Member").classes("text-base font-medium")
        ui.label(
            "Front-loaded staffing data flows into the Writer Team's "
            "cached prefix so prose names + allocations come out "
            "right the first time."
        ).classes("text-xs opacity-70 pb-2")

        # Role name — searchable suggestion list from
        # company_profile.key_personnel, but the user can type any
        # custom role (e.g. RFP-specific titles) via add-unique. Pre-
        # include the saved value so edit mode opens cleanly even
        # when the saved role isn't currently in the profile.
        initial_role = (initial.get("role_name") or "").strip() or None
        role_select_options = dict(role_options)
        if initial_role and initial_role not in role_select_options:
            role_select_options[initial_role] = initial_role
        role_input = (
            ui.select(
                options=role_select_options,
                label="Role name",
                value=initial_role,
                with_input=True,
                new_value_mode="add-unique",
            )
            .props('outlined dense hint="Pick from existing roles or type a new one."')
            .classes("w-full")
        )

        with ui.row().classes("w-full gap-3 items-center"):
            ui.label("Person kind").classes("text-xs uppercase opacity-70")
            kind_radio = ui.radio(
                {
                    "named": "Named",
                    "tbh": "To-be-hired",
                    "sub": "Subcontractor",
                },
                value=initial.get("person_kind") or "named",
            ).props("inline dense")

        # Searchable dropdown of canonical names from
        # company_profile.key_personnel + KB resume filenames.
        # with_input + new_value_mode 'add-unique' lets the user
        # type a new name (e.g., a recent hire not yet in the
        # profile, or 'Sub: <partner>') while still surfacing the
        # known list. The 'To Be Hired' sentinel is pinned to the
        # top — picking it auto-flips the kind radio to 'tbh'.
        initial_person = initial.get("assigned_person") or None
        # Quasar select rejects values that aren't in the options
        # by default; pre-include the initial value so edit mode
        # opens cleanly even when the saved person isn't currently
        # in key_personnel or KB. Built as a dict {value: label} so
        # the role suffix shows in the dropdown without affecting
        # what's stored as assigned_person.
        select_options: dict[str, str] = {TBH_OPTION: TBH_OPTION}
        select_options.update(profile_names)
        if initial_person and initial_person not in select_options:
            select_options[initial_person] = initial_person

        person_input = (
            ui.select(
                options=select_options,
                label="Assigned person",
                value=initial_person,
                with_input=True,
                new_value_mode="add-unique",
            )
            .props(
                "outlined dense clearable "
                'hint="Pick from list or type a new name '
                "(incl. 'TBH' or 'Sub: Partner')\""
            )
            .classes("w-full")
        )

        # Secondary dropdown — surfaces ONLY when the chosen person
        # has multiple roles in the profile (e.g., Taylor M. Brooks
        # = Requirements Manager AND Salesforce/Servicemax
        # Administrator). The user picks which role's data to
        # autofill from. Hidden by default; the autofill handler
        # toggles visibility based on get_person_roles_in_profile.
        profile_role_select = (
            ui.select(
                options={},
                label="Profile role (controls autofill)",
                value=None,
                with_input=False,
            )
            .props(
                "outlined dense "
                'hint="This person fills more than one role in the '
                "profile — pick which role to use for the bio + "
                'labor-category autofill."'
            )
            .classes("w-full")
        )
        profile_role_select.set_visibility(False)

        with ui.row().classes("w-full gap-3"):
            # Labor category — searchable suggestion list from BOTH
            # canonical pricing sources (handoff: two-shape data lives
            # in pricing_rules.labor_catalog AND
            # company_profile.labor_rate_card.categories). User can
            # type a custom title via add-unique for RFP-driven
            # categories not in either source. Pre-include the saved
            # value so edit mode opens cleanly.
            initial_cat = (initial.get("labor_category") or "").strip() or None
            cat_select_options = dict(cat_options)
            if initial_cat and initial_cat not in cat_select_options:
                cat_select_options[initial_cat] = initial_cat
            cat_input = (
                ui.select(
                    options=cat_select_options,
                    label="Labor category (GSA OLM)",
                    value=initial_cat,
                    with_input=True,
                    new_value_mode="add-unique",
                )
                .props('outlined dense hint="Pick from rate card or type a custom title."')
                .classes("flex-1")
            )
            wage_input = (
                ui.input(
                    "Salary",
                    value=initial.get("wage_band") or "",
                    placeholder="170K",
                )
                .props("outlined dense")
                .classes("w-32")
            )

        # Autofill state — track the last name we looked up so blur
        # events on the person field don't redundantly re-fetch the
        # same profile entry every time the user tabs around.
        last_lookup_name: dict = {"value": ""}

        def _apply_autofill_for_role(
            canonical: str,
            selected_role: str | None,
            *,
            notify: bool,
        ) -> None:
            """Inner — given a canonical name and an optional
            selected role, run lookup_person_in_profile and stamp
            the resulting bio / experience / labor_category onto
            the dialog fields. Used both by the name-picker (when
            a person is first chosen) and the role-picker (when
            the user changes which role to autofill from)."""
            from app.services.team import lookup_person_in_profile

            found = lookup_person_in_profile(
                canonical,
                selected_role=selected_role,
            )
            if not found:
                return
            yrs = found.get("years_experience")
            if yrs is not None:
                yrs_input.set_value(str(yrs))
            bio = found.get("bio_summary") or ""
            if bio:
                bio_input.set_value(bio)
            suggested_cat = found.get("suggested_labor_category")
            if suggested_cat:
                # cat_input is now a ui.select. Programmatic set_value
                # only sticks when the value is already in `options`;
                # add-unique runs on user input, not on set_value. So
                # add the autofilled value to options first if missing.
                if suggested_cat not in cat_input.options:
                    cat_input.options[suggested_cat] = suggested_cat
                    cat_input.update()
                cat_input.set_value(suggested_cat)
            if notify:
                msg = f"Auto-filled from profile: {canonical}"
                if yrs is not None:
                    msg += f" ({yrs} yrs)"
                if selected_role:
                    msg += f" — {selected_role}"
                ui.notify(msg, type="positive", timeout=2500)

        def _autofill_from_profile() -> None:
            """Fires when the user picks a person from the dropdown
            (or types a new value). When the chosen value matches a
            company_profile.key_personnel entry, populate the rest
            of the card. Silent no-op when the name doesn't match
            (a new hire / TBH / Sub).

            For multi-role people, surface the second 'Profile role'
            dropdown so the user picks which role's data to use;
            the role-picker drives the actual field population via
            _apply_autofill_for_role."""
            from app.services.team import (
                get_person_roles_in_profile,
                lookup_person_in_profile,
            )

            raw = person_input.value
            name = (raw or "").strip() if isinstance(raw, str) else ""
            if not name:
                last_lookup_name["value"] = ""
                profile_role_select.set_visibility(False)
                return
            # TBH sentinel — flip the radio and skip profile lookup.
            if name == TBH_OPTION:
                if (kind_radio.value or "").lower() != "tbh":
                    kind_radio.set_value("tbh")
                last_lookup_name["value"] = name
                profile_role_select.set_visibility(False)
                return
            kind = (kind_radio.value or "named").lower()
            if kind != "named":
                last_lookup_name["value"] = ""
                profile_role_select.set_visibility(False)
                return
            if name.lower() == last_lookup_name["value"].lower():
                # Same name as the previous trigger; don't clobber
                # any field edits the user made since.
                return
            last_lookup_name["value"] = name

            # Probe the profile to find canonical name + role list.
            roles = get_person_roles_in_profile(name)
            base = lookup_person_in_profile(name)
            if not base:
                profile_role_select.set_visibility(False)
                return

            canonical = base.get("name") or name
            if canonical and canonical != person_input.value:
                # Make sure the canonical name is selectable in the
                # dropdown options before assigning it (Quasar
                # rejects values not in the options dict).
                if canonical not in person_input.options:
                    new_opts = dict(person_input.options or {})
                    new_opts[canonical] = canonical
                    person_input.options = new_opts
                    person_input.update()
                person_input.set_value(canonical)
                last_lookup_name["value"] = canonical

            # Multi-role people get the second dropdown. Single-role
            # (or no role) people skip it and we autofill straight
            # away with the merged data.
            if len(roles) >= 2:
                profile_role_select.options = {r: r for r in roles}
                profile_role_select.set_value(roles[0])
                profile_role_select.set_visibility(True)
                profile_role_select.update()
                _apply_autofill_for_role(
                    canonical,
                    roles[0],
                    notify=True,
                )
            else:
                profile_role_select.set_visibility(False)
                _apply_autofill_for_role(
                    canonical,
                    roles[0] if roles else None,
                    notify=True,
                )

        def _on_profile_role_change() -> None:
            """User picked a different role for the same person —
            re-run the autofill against that role only."""
            raw = person_input.value
            canonical = (raw or "").strip() if isinstance(raw, str) else ""
            picked_role = profile_role_select.value
            if not canonical or not picked_role:
                return
            _apply_autofill_for_role(
                canonical,
                str(picked_role),
                notify=False,
            )

        person_input.on_value_change(lambda _e: _autofill_from_profile())
        profile_role_select.on_value_change(lambda _e: _on_profile_role_change())

        def _on_kind_change() -> None:
            """When the user flips the Person Kind radio to TBH,
            lock down the Assigned-person field — no name is
            allowed for to-be-hired roles. Flipping back to Named
            or Subcontractor re-enables it. Also hides the multi-
            role secondary dropdown since no profile lookup will
            apply for a TBH role."""
            kind = (kind_radio.value or "named").lower()
            if kind == "tbh":
                # Clear any selected name and disable the field so
                # the user can't type/pick one.
                person_input.set_value(None)
                person_input.props(add="disable")
                profile_role_select.set_visibility(False)
                last_lookup_name["value"] = ""
            else:
                person_input.props(remove="disable")

        kind_radio.on_value_change(lambda _e: _on_kind_change())
        # Initial paint — if the dialog opened in edit mode for a
        # TBH row, reflect that lock on the assigned-person field.
        if (initial.get("person_kind") or "named").lower() == "tbh":
            person_input.props(add="disable")

        # Run once on open in case the dialog opened with a value
        # already (edit mode, or pre-filled from a future agent).
        if initial_person:
            last_lookup_name["value"] = initial_person

        with ui.row().classes("w-full gap-3 items-center"):
            ui.label("Time allocation").classes("text-xs uppercase opacity-70")
            pct_slider = (
                ui.slider(
                    min=0,
                    max=100,
                    step=5,
                    value=int(initial.get("time_allocation_pct") or 0),
                )
                .props("label-always")
                .classes("flex-1")
            )
            pct_label = ui.label(f"{int(initial.get('time_allocation_pct') or 0)}%").classes(
                "text-sm font-medium w-12 text-right"
            )

            def _on_pct(e, _slider=pct_slider, _label=pct_label) -> None:
                _label.text = f"{int(_slider.value or 0)}%"

            pct_slider.on_value_change(_on_pct)

        with ui.row().classes("w-full gap-3"):
            yrs_input = (
                ui.input(
                    "Experience (yrs)",
                    value=str(initial.get("experience_years") or "")
                    if initial.get("experience_years") is not None
                    else "",
                    placeholder="12",
                )
                .props("outlined dense type=number")
                .classes("w-40")
            )
            phases_value = ", ".join(str(p) for p in (initial.get("phases_active") or []))
            phases_input = (
                ui.input(
                    "Active phases (comma-separated)",
                    value=phases_value,
                    placeholder="Phase 1, Phase 2, Phase 3",
                )
                .props("outlined dense")
                .classes("flex-1")
            )

        bio_input = (
            ui.textarea(
                "Bio summary (1-2 sentences)",
                value=initial.get("bio_summary") or "",
                placeholder=("PMP-certified; led NC SBI 2024 hosting refresh."),
            )
            .props("outlined dense autogrow")
            .classes("w-full")
        )

        with ui.row().classes("justify-end gap-2 pt-2 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def _save() -> None:
                role = (role_input.value or "").strip()
                if not role:
                    ui.notify(
                        "Role name is required.",
                        type="warning",
                        timeout=3000,
                    )
                    return
                phases_raw = (phases_input.value or "").strip()
                phases_list = [p.strip() for p in phases_raw.split(",") if p.strip()] if phases_raw else []
                yrs_raw = (yrs_input.value or "").strip()
                try:
                    yrs_val = int(yrs_raw) if yrs_raw else None
                except ValueError:
                    yrs_val = None
                payload = {
                    "role_name": role,
                    "person_kind": kind_radio.value or "named",
                    "assigned_person": person_input.value or "",
                    "labor_category": cat_input.value or "",
                    "wage_band": wage_input.value or "",
                    "time_allocation_pct": int(pct_slider.value or 0),
                    "experience_years": yrs_val,
                    "phases_active": phases_list,
                    "bio_summary": bio_input.value or "",
                }
                if is_edit:
                    ok = update_team_member(member["id"], payload)
                    if not ok:
                        ui.notify(
                            "Update failed — member not found.",
                            type="negative",
                            timeout=4000,
                        )
                        return
                    ui.notify(
                        "Saved.",
                        type="positive",
                        timeout=2000,
                    )
                else:
                    new_id = add_team_member(proposal_id, payload)
                    if new_id is None:
                        ui.notify(
                            "Add failed — proposal not found or role_name empty.",
                            type="negative",
                            timeout=4000,
                        )
                        return
                    ui.notify(
                        "Added.",
                        type="positive",
                        timeout=2000,
                    )
                dialog.close()
                on_saved()

            ui.button(
                "Save",
                icon="save",
                on_click=_save,
            ).props("color=primary")

    dialog.open()


def _open_team_composer_dialog(
    proposal_id: int,
    *,
    on_applied,
) -> None:
    """Team Composer (AI) preview dialog.

    Calls the team_composer agent in a background thread (so the
    NiceGUI event loop stays responsive), renders the proposed
    roles + summary + per-role rationale, and on Apply atomically
    replaces the existing roster via app.services.team.replace_team.
    The user then assigns specific people to each role via the
    existing Edit dialog.
    """
    from app.jobs.team_composer import propose_team_composition
    from app.services.team import get_team_members, replace_team

    # State keeps the dialog mode + agent result. Mode transitions:
    #   loading → preview → applied (success) | error
    state: dict = {
        "mode": "loading",
        "summary": "",
        "roles": [],
        "n_existing": len(get_team_members(proposal_id)),
        "error": "",
    }

    with ui.dialog() as dialog, ui.card().classes("w-[820px] max-w-[95vw]"):
        ui.label("Propose Team (AI)").classes("text-base font-medium")
        ui.label(
            "Sonnet 4.6 reads the RFP scope, outline, compliance "
            "matrix, and Quadratic's labor catalog, then proposes "
            "a delivery team — roles, labor categories, and time "
            "allocations. Review below; on Apply the proposal "
            "REPLACES the current roster. You then assign specific "
            "people to each role via the Edit dialog."
        ).classes("text-xs opacity-70 pb-2")

        @ui.refreshable
        def render_body() -> None:
            mode = state["mode"]
            if mode == "loading":
                with ui.row().classes("items-center gap-2 py-6"):
                    ui.spinner(size="md").classes("text-primary")
                    ui.label("Composing team… (Sonnet 4.6, ~10-20s)").classes("text-sm opacity-70")
                return
            if mode == "error":
                ui.label(f"Team composer failed: {state['error']}").classes("text-sm text-red-600")
                return
            if mode == "applied":
                with ui.row().classes("items-center gap-2 py-2"):
                    ui.icon("check_circle").classes("text-green-600 text-2xl")
                    ui.label(
                        f"Applied — {len(state['roles'])} role(s) "
                        f"are now in the roster. Click Edit on "
                        f"each one to assign a person."
                    ).classes("text-sm font-medium")
                return

            # mode == "preview"
            if state["summary"]:
                ui.label("Team summary").classes("text-xs uppercase opacity-60 pt-1")
                ui.label(state["summary"]).classes("text-sm pb-2")

            if state["n_existing"]:
                with ui.card().classes("w-full bg-amber-50 border-l-4 border-amber-400 mb-2"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("warning").classes("text-amber-700")
                        ui.label(
                            f"Applying will REPLACE the current "
                            f"{state['n_existing']} roster "
                            f"member(s). Their assigned people, "
                            f"bios, and salaries will be lost."
                        ).classes("text-xs text-amber-900")

            if not state["roles"]:
                ui.label(
                    "Agent returned no roles — try regenerating, or check the outline / compliance matrix."
                ).classes("text-sm opacity-80")
                return

            ui.label(f"{len(state['roles'])} proposed role(s):").classes("text-xs opacity-70 pt-1")
            for r in state["roles"]:
                with ui.card().classes("w-full border border-slate-200 mb-2"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.label(r["role_name"]).classes("text-sm font-medium")
                        if r.get("labor_category"):
                            ui.label(r["labor_category"]).classes("text-xs px-2 py-0.5 bg-slate-100 rounded")
                        ui.label(f"{r.get('time_allocation_pct') or 0}% time").classes(
                            "text-xs px-2 py-0.5 bg-blue-50 text-blue-800 rounded"
                        )
                        phases = r.get("phases_active") or []
                        if phases:
                            ui.label(f"Phases: {', '.join(str(p) for p in phases)}").classes(
                                "text-xs px-2 py-0.5 bg-slate-100 rounded"
                            )
                    if r.get("bio_summary"):
                        ui.label(f"Role: {r['bio_summary']}").classes("text-xs opacity-80 pt-1")
                    if r.get("rationale"):
                        ui.label(f"Why: {r['rationale']}").classes("text-xs opacity-60 italic pt-1")

        render_body()

        @ui.refreshable
        def render_buttons() -> None:
            mode = state["mode"]
            with ui.row().classes("items-center justify-end gap-2 pt-2 w-full"):
                if mode == "applied":
                    ui.button(
                        "Close",
                        on_click=dialog.close,
                    ).props("color=primary")
                    return
                if mode in ("loading", "error"):
                    ui.button(
                        "Close",
                        on_click=dialog.close,
                    ).props("flat")
                    if mode == "error":
                        ui.button(
                            "Retry",
                            icon="refresh",
                            on_click=lambda: _kick_off_sync(),
                        ).props("color=primary")
                    return
                # mode == "preview"
                ui.button(
                    "Cancel",
                    on_click=dialog.close,
                ).props("flat")
                ui.button(
                    "Regenerate",
                    icon="refresh",
                    on_click=lambda: _kick_off_sync(),
                ).props("outline")
                apply_label = (
                    f"Apply (replaces {state['n_existing']})" if state["n_existing"] else "Apply to Roster"
                )
                ui.button(
                    apply_label,
                    icon="check",
                    on_click=_on_apply,
                ).props("color=positive")

        def _on_apply() -> None:
            try:
                n = replace_team(
                    proposal_id,
                    state["roles"],
                )
            except Exception as exc:
                ui.notify(
                    f"Apply failed: {type(exc).__name__}: {exc}",
                    type="negative",
                    timeout=5000,
                )
                return
            state["mode"] = "applied"
            render_body.refresh()
            render_buttons.refresh()
            ui.notify(
                f"Roster replaced with {n} proposed role(s). Click Edit on each card to assign a person.",
                type="positive",
                multi_line=True,
                timeout=5000,
            )
            try:
                on_applied()
            except Exception:
                log.exception("team-composer on_applied raised")

        async def _kick_off() -> None:
            state["mode"] = "loading"
            state["error"] = ""
            render_body.refresh()
            render_buttons.refresh()
            try:
                result = await asyncio.to_thread(
                    propose_team_composition,
                    proposal_id,
                )
            except Exception as exc:
                state["mode"] = "error"
                state["error"] = f"{type(exc).__name__}: {exc}"
                render_body.refresh()
                render_buttons.refresh()
                return
            if result is None:
                state["mode"] = "error"
                state["error"] = "Proposal not found."
                render_body.refresh()
                render_buttons.refresh()
                return
            state["summary"] = result.get("summary") or ""
            state["roles"] = list(result.get("roles") or [])
            state["n_existing"] = int(result.get("n_existing_members") or 0)
            state["mode"] = "preview"
            render_body.refresh()
            render_buttons.refresh()

        def _kick_off_sync() -> None:
            # Wrapper so on_click can call into the async kickoff.
            asyncio.create_task(_kick_off())

        render_buttons()

        ui.timer(0.1, _kick_off, once=True)

    dialog.open()


def _render_team_tab(
    proposal_id: int,
    *,
    on_state_change=None,
) -> None:
    """Team tab — manual roster entry with approval gate.

    Front-loads named personnel + time allocations + labor categories
    BEFORE the Writer Team drafts. The approved roster is injected
    into the writer's cached prefix so the writer stops emitting
    [NEEDS_HUMAN] for staffing details. Until the user clicks
    Approve Team, the roster does NOT appear in the cached prefix
    (the writer falls back to NEEDS_HUMAN behavior).
    """
    from app.services.team import (
        approve_team,
        delete_team_member,
        get_team_approval_state,
        get_team_members,
    )

    @ui.refreshable
    def render() -> None:
        members = get_team_members(proposal_id)
        approval = get_team_approval_state(proposal_id)
        approved_at = approval.get("approved_at")
        n_members = approval.get("member_count", 0)
        n_unfilled = approval.get("n_unfilled", 0)
        unfilled_role_names = list(approval.get("unfilled_role_names") or [])

        def _after_change() -> None:
            render.refresh()
            if on_state_change is not None:
                on_state_change()

        # Header card
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full flex-wrap gap-3"):
                with ui.column().classes("gap-0 flex-1"):
                    ui.label("Team Composition").classes("text-base font-medium")
                with ui.row().classes("items-center gap-2"):
                    if approved_at is not None and n_unfilled == 0:
                        ui.chip(
                            f"Approved {approved_at:%b %d, %H:%M UTC}",
                            icon="verified",
                        ).props("color=positive outline")
                    elif n_unfilled:
                        # Highlight the blocking issue: roles need
                        # a name or TBH selection before approval
                        # is possible.
                        preview = ", ".join(unfilled_role_names[:3])
                        if len(unfilled_role_names) > 3:
                            preview += f" (+{len(unfilled_role_names) - 3} more)"
                        ui.chip(
                            f"{n_unfilled} role"
                            f"{'s' if n_unfilled != 1 else ''} "
                            f"need a name or TBH: {preview}",
                            icon="error",
                        ).props("color=red outline")
                    elif n_members:
                        ui.chip(
                            f"Pending approval ({n_members} member{'s' if n_members != 1 else ''})",
                            icon="schedule",
                        ).props("color=amber outline")
                    else:
                        ui.chip(
                            "Empty — add team members to start",
                            icon="info",
                        ).props("color=grey outline")

                    ui.button(
                        "Propose Team (AI)",
                        icon="auto_awesome",
                        on_click=lambda: _open_team_composer_dialog(
                            proposal_id,
                            on_applied=_after_change,
                        ),
                    ).props("color=primary outline").tooltip(
                        "Sonnet 4.6 reads the RFP scope + outline + "
                        "compliance matrix and proposes a delivery "
                        "team (roles + labor categories + time "
                        "allocations). Replaces the current roster "
                        "on Apply. ~$0.05-0.15 per call."
                    )
                    ui.button(
                        "Add Team Member",
                        icon="person_add",
                        on_click=lambda: _open_team_member_dialog(
                            proposal_id,
                            None,
                            on_saved=_after_change,
                        ),
                    ).props("color=primary")

                    if n_members:
                        approve_label = "Re-approve Team" if approved_at is not None else "Approve Team"

                        def _on_approve() -> None:
                            # Belt-and-suspenders: even though the
                            # button is disabled when n_unfilled > 0,
                            # also surface the message on click in
                            # case a stale state slipped through.
                            if n_unfilled:
                                preview = ", ".join(unfilled_role_names[:5])
                                ui.notify(
                                    f"{n_unfilled} role"
                                    f"{'s' if n_unfilled != 1 else ''} "
                                    f"still need a name or "
                                    f"'To Be Hired' selected: "
                                    f"{preview}.",
                                    type="warning",
                                    multi_line=True,
                                    timeout=6000,
                                )
                                return
                            ok = approve_team(proposal_id)
                            if ok:
                                ui.notify(
                                    "Team approved. Future writer regenerates will use the roster.",
                                    type="positive",
                                    multi_line=True,
                                    timeout=4000,
                                )
                                _after_change()
                            else:
                                ui.notify(
                                    "Could not approve — empty roster.",
                                    type="warning",
                                    timeout=3000,
                                )

                        approve_btn = ui.button(
                            approve_label,
                            icon="verified",
                            on_click=_on_approve,
                        )
                        approve_props = "color=positive"
                        if approved_at is not None and n_unfilled == 0:
                            approve_props += " outline"
                        if n_unfilled:
                            approve_props += " disable"
                            approve_btn.tooltip(
                                f"{n_unfilled} role"
                                f"{'s' if n_unfilled != 1 else ''} "
                                f"need a name or 'To Be Hired' "
                                f"before the team can be approved."
                            )
                        approve_btn.props(approve_props)

        # Empty state
        if not members:
            _empty_state(
                "No team members yet. Click Add Team Member to start building the roster.",
                icon="groups",
            )
            return

        # Member cards
        with ui.column().classes("w-full gap-2"):
            for m in members:
                kind = m.get("person_kind") or "named"
                kind_cls = _PERSON_KIND_COLOR.get(
                    kind,
                    "bg-slate-50 border-slate-200 text-slate-800",
                )
                kind_label = _PERSON_KIND_LABEL.get(kind, kind)
                with ui.card().classes("w-full border border-slate-200"):
                    with ui.row().classes("items-start justify-between w-full"):
                        with ui.column().classes("gap-1 flex-1"):
                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                ui.label(m["role_name"]).classes("text-base font-medium")
                                ui.label(kind_label).classes(
                                    f"text-xs uppercase px-2 py-0.5 rounded border {kind_cls}"
                                )
                            person = m.get("assigned_person") or "(unassigned)"
                            yrs = (
                                f" · {m['experience_years']} yrs exp"
                                if m.get("experience_years") is not None
                                else ""
                            )
                            ui.label(f"{person}{yrs}").classes("text-sm")
                            with ui.row().classes("items-center gap-2 flex-wrap pt-1"):
                                if m.get("labor_category"):
                                    ui.label(m["labor_category"]).classes(
                                        "text-xs px-2 py-0.5 bg-slate-100 rounded"
                                    )
                                if m.get("wage_band"):
                                    ui.label(m["wage_band"]).classes(
                                        "text-xs px-2 py-0.5 bg-slate-100 rounded"
                                    )
                                ui.label(f"{m.get('time_allocation_pct') or 0}% time").classes(
                                    "text-xs px-2 py-0.5 bg-blue-50 text-blue-800 rounded"
                                )
                                phases = m.get("phases_active") or []
                                if phases:
                                    ui.label(f"Phases: {', '.join(str(p) for p in phases)}").classes(
                                        "text-xs px-2 py-0.5 bg-slate-100 rounded"
                                    )
                            if m.get("bio_summary"):
                                ui.label(m["bio_summary"]).classes("text-xs opacity-70 italic pt-1")

                        with ui.row().classes("items-center gap-1"):
                            ui.button(
                                icon="edit",
                                on_click=lambda _, _m=m: _open_team_member_dialog(
                                    proposal_id,
                                    _m,
                                    on_saved=_after_change,
                                ),
                            ).props("flat dense round").tooltip("Edit")

                            def _on_delete(_, _m=m) -> None:
                                ok = delete_team_member(_m["id"])
                                if ok:
                                    ui.notify(
                                        f"Removed {_m['role_name']}.",
                                        type="positive",
                                        timeout=2000,
                                    )
                                    _after_change()
                                else:
                                    ui.notify(
                                        "Delete failed.",
                                        type="negative",
                                        timeout=3000,
                                    )

                            ui.button(
                                icon="delete",
                                on_click=_on_delete,
                            ).props("flat dense round").tooltip("Remove")

    render()
