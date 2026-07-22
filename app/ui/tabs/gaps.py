"""Proposal Review > Gaps tab + Teaming Strategy view.

Two surfaces in one module:
  - `_render_gaps_tab` — Shortfall Strategist output grouped by
    severity/category, with per-gap action cards (set framing,
    pick mitigation, choose partner, mark resolved).
  - `_render_teaming_strategy_tab` — cross-gap teaming view:
    matrix (partners x gaps coverage) + per-partner aggregate cards
    with click-through to a partner profile dialog.

The teaming view is rendered as a sub-section inside the Gaps tab,
so all its helpers (`_aggregate_teaming_partners`, `_willingness_for`,
`_render_teaming_matrix`, `_render_partner_aggregate_card`,
`_open_partner_profile`) belong here too.
"""

from __future__ import annotations

import logging

from nicegui import ui
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import ComplianceMatrixItem, GapAnalysis
from app.services.framing import (
    apply_framing_to_unaddressed_gaps,
    get_framing,
    pick_mitigation_for_framing,
    set_framing,
    update_gap_resolution,
)
from app.ui._shared import _empty_state

log = logging.getLogger(__name__)


# Severity → visual styling map for Gaps tab cards / chips.
# (icon, accent_color, card_bg, border_class)
_SEVERITY_VISUAL = {
    "deal_breaker": ("error", "red-7", "red-50", "border-red-500"),
    "major": ("warning", "orange-7", "orange-50", "border-orange-400"),
    "minor": ("info", "amber-7", "amber-50", "border-amber-300"),
    "technical": ("memory", "blue-7", "blue-50", "border-blue-400"),
}

# Display order — deal-breakers first, then technical (often most
# actionable), then firm severity descending.
_SEVERITY_ORDER = ("deal_breaker", "technical", "major", "minor")


_UNSET = object()


def _aggregate_teaming_partners(gaps: list[dict]) -> list[dict]:
    """Walk every gap's mitigation_options; collect all partner suggestions
    deduped by name; record which gaps each partner could solve and which
    mitigation option / severity context they appear in.

    Returns a list of partner aggregates sorted by:
      1. gap count (descending — highest leverage first)
      2. willingness (active library partners > prospects > cold market leads)
      3. name (alphabetical tiebreaker)
    """
    by_name: dict[str, dict] = {}
    for g in gaps:
        for opt_idx, opt in enumerate(g.get("mitigation_options", []) or []):
            partners = opt.get("partner_suggestions") or []
            for p in partners:
                name = (p.get("name") or "").strip()
                if not name:
                    continue
                # First time we see this partner, capture profile + flags
                # from this suggestion (later occurrences in other gaps may
                # have slightly different rationale text, but the firm-level
                # profile is identical).
                entry = by_name.setdefault(
                    name,
                    {
                        "name": name,
                        "from_library": p.get("from_library", True),
                        "confirmed": p.get("confirmed", False),
                        "capability_focus": p.get("capability_focus"),
                        "profile": p.get("profile") or {},
                        "raw_partner": p,
                        "gaps": [],
                    },
                )
                entry["gaps"].append(
                    {
                        "gap_pk": g["id"],
                        "gap_id": g["gap_id"],
                        "req_text": g["req_text"],
                        "severity": g["severity"],
                        "mitigation_index": opt_idx,
                        "mitigation_approach": opt.get("approach", ""),
                        "is_recommended": opt_idx == g.get("recommended_index"),
                        "is_user_selected_mitigation": opt_idx == g.get("selected_index"),
                        "is_user_selected_partner": (
                            g.get("selected_partner") == name and opt_idx == g.get("selected_index")
                        ),
                        "fit_rationale": p.get("fit_rationale", ""),
                        # Dual-pipeline provenance (set by the
                        # teaming_consolidator). Empty list / False on
                        # legacy single-provider data.
                        "confirmed_by": list(p.get("confirmed_by") or []),
                        "needs_review": bool(p.get("needs_review")),
                    }
                )
    result = list(by_name.values())
    result.sort(
        key=lambda x: (
            -len(x["gaps"]),
            not x["confirmed"],
            not x["from_library"],
            x["name"].lower(),
        )
    )
    return result


def _willingness_for(partner_agg: dict) -> tuple[str, str, str]:
    """(label, qcolor, icon) for the willingness chip."""
    if not partner_agg.get("from_library"):
        return ("Cold market lead", "blue-grey-6", "ac_unit")
    if partner_agg.get("confirmed"):
        return ("Active partner", "green-7", "verified_user")
    return ("Warm prospect", "amber-7", "thermostat_auto")


def _render_teaming_strategy_tab(proposal_id: int, gaps: list[dict]) -> None:
    """Cross-gap teaming view: matrix (compare partners by gap coverage) +
    cards (per-partner detail with click-through to profile).

    Pure UI projection of existing GapAnalysis data — no new schema needed.

    Includes the "Run Teaming Research" button that triggers the
    Gemini-grounded teaming researcher on demand. Removed from the
    automatic intake pipeline (was wasted spend on self-perform-
    everywhere proposals); user now opts in here when they decide
    teaming is on the table.
    """
    # Count gaps that have at least one teaming-style mitigation option —
    # those are the ones the Teaming Researcher would actually process.
    # Mirrors _is_teaming_option in app.jobs.intake.
    n_teaming_gaps = 0
    for g in gaps:
        for opt in g.get("mitigation_options") or []:
            approach = (opt.get("approach") or "").lower()
            if approach.startswith("teaming"):
                n_teaming_gaps += 1
                break

    # Top action card — always visible. Lets the user run the Gemini-
    # grounded teaming researcher on demand.
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full flex-wrap gap-3"):
            with ui.column().classes("gap-0 flex-1"):
                ui.label("Teaming research").classes("text-base font-medium")
                ui.label(
                    "Gemini-grounded web search produces specific partner "
                    "names + citations for each gap with a teaming-style "
                    "mitigation. Optional — only worth running once you've "
                    "decided teaming is on the table."
                ).classes("text-xs opacity-70")
            est_cost = n_teaming_gaps * 0.05
            est_label = (
                f"~{n_teaming_gaps} gap(s) × ~$0.05 ≈ ${est_cost:.2f}"
                if n_teaming_gaps
                else "no teaming-style gaps to research"
            )
            ui.label(est_label).classes("text-xs font-mono opacity-70")

            def _kick_off_teaming() -> None:
                from app.jobs.intake import spawn_teaming_research

                spawn_teaming_research(proposal_id)
                ui.notify(
                    f"Teaming research started for {n_teaming_gaps} gap(s) — "
                    f"watch progress on the Run Progress page; partner cards "
                    f"refresh on this tab when it completes.",
                    type="positive",
                    multi_line=True,
                    timeout=6000,
                )
                ui.navigate.to(f"/proposals/{proposal_id}/progress")

            btn = ui.button(
                "Run Teaming Research",
                icon="search",
                on_click=_kick_off_teaming,
            ).props("color=primary")
            if n_teaming_gaps == 0:
                btn.props("disable").tooltip(
                    "No gaps have teaming-style mitigation options. "
                    "If you want to consider teaming for a gap, edit "
                    "the gap or re-run shortfall analysis."
                )

    partners = _aggregate_teaming_partners(gaps)
    if not partners:
        _empty_state(
            "No teaming partners surfaced yet. Click 'Run Teaming Research' "
            "above (when there are teaming-style gaps) to populate partner "
            "suggestions with Gemini-grounded web search.",
            icon="handshake",
        )
        return

    n_partners = len(partners)
    gap_ids_with_teaming: set[str] = set()
    for p in partners:
        for ge in p["gaps"]:
            gap_ids_with_teaming.add(ge["gap_id"])

    # Summary header
    with ui.row().classes("flex-wrap gap-2 pt-2"):
        ui.chip(
            f"{n_partners} unique partner{'s' if n_partners != 1 else ''}",
            icon="handshake",
        ).props("color=primary text-color=white")
        ui.chip(
            f"covering {len(gap_ids_with_teaming)} gap{'s' if len(gap_ids_with_teaming) != 1 else ''}",
            icon="hub",
        ).props("color=blue-grey-6 text-color=white")
        n_active = sum(1 for p in partners if p["confirmed"])
        n_prospect = sum(1 for p in partners if p["from_library"] and not p["confirmed"])
        n_cold = sum(1 for p in partners if not p["from_library"])
        if n_active:
            ui.chip(f"{n_active} active", icon="verified_user").props("color=green-7 text-color=white")
        if n_prospect:
            ui.chip(f"{n_prospect} warm prospect", icon="thermostat_auto").props(
                "color=amber-7 text-color=white"
            )
        if n_cold:
            ui.chip(f"{n_cold} cold lead", icon="ac_unit").props("color=blue-grey-6 text-color=white")
    ui.label(
        "Highest-leverage partners first. Click a name for the full profile; "
        "click 'Select for gap' to set a partner as your choice for a specific gap."
    ).classes("text-xs opacity-60")

    # Matrix view
    with ui.expansion(
        f"Comparison matrix ({len(gap_ids_with_teaming)} gaps × {n_partners} partners)",
        icon="grid_on",
        value=(n_partners <= 12),
    ).classes("w-full"):
        _render_teaming_matrix(partners, gap_ids_with_teaming)

    ui.separator().classes("my-3")
    ui.label("Partner detail").classes("text-base font-medium")

    # Per-partner cards
    for p in partners:
        _render_partner_aggregate_card(p)


def _render_teaming_matrix(partners: list[dict], gap_ids: set[str]) -> None:
    """Compact gap × partner matrix. Cell = ✓ if partner could solve that gap.
    Bottom row totals per partner."""
    # Cap at 12 partners for table width — beyond that, matrix becomes
    # unreadable. User can drop to cards for the long tail.
    DISPLAY_CAP = 12
    visible = partners[:DISPLAY_CAP]
    truncated = max(0, len(partners) - DISPLAY_CAP)

    # Build {gap_id → set(partner_name)} from the partner aggregates.
    coverage: dict[str, set[str]] = {gid: set() for gid in gap_ids}
    gap_severity: dict[str, str] = {}
    gap_text: dict[str, str] = {}
    for p in partners:
        for ge in p["gaps"]:
            coverage.setdefault(ge["gap_id"], set()).add(p["name"])
            gap_severity[ge["gap_id"]] = ge["severity"]
            gap_text[ge["gap_id"]] = ge["req_text"]

    # Sort gap rows by severity (deal_breaker first), then gap_id.
    sev_rank = {"deal_breaker": 0, "technical": 1, "major": 2, "minor": 3}
    sorted_gap_ids = sorted(gap_ids, key=lambda gid: (sev_rank.get(gap_severity.get(gid, ""), 9), gid))

    columns = [
        {"name": "gap", "label": "Gap", "field": "gap", "align": "left"},
        {"name": "sev", "label": "Sev", "field": "sev", "align": "left"},
    ] + [
        {"name": f"p{i}", "label": p["name"], "field": f"p{i}", "align": "center"}
        for i, p in enumerate(visible)
    ]
    rows: list[dict] = []
    for gid in sorted_gap_ids:
        row = {
            "gap": gid,
            "sev": gap_severity.get(gid, ""),
        }
        cov = coverage.get(gid, set())
        for i, p in enumerate(visible):
            row[f"p{i}"] = "✓" if p["name"] in cov else ""
        rows.append(row)

    # Totals row
    totals = {"gap": "TOTAL", "sev": ""}
    for i, p in enumerate(visible):
        totals[f"p{i}"] = str(len(p["gaps"]))
    rows.append(totals)

    ui.table(columns=columns, rows=rows, row_key="gap").classes("w-full")
    if truncated:
        ui.label(f"({truncated} additional partner(s) not shown — see cards below.)").classes(
            "text-xs opacity-60 italic pt-1"
        )


def _render_partner_aggregate_card(p: dict) -> None:
    """One card per partner with willingness, contact, and the gaps they could fill."""
    label, qcolor, qicon = _willingness_for(p)
    contact = (p.get("profile") or {}).get("contact") or {}
    capability_focus = p.get("capability_focus")

    # Aggregate dual-pipeline provenance across this partner's gap
    # rows so the user can see at a glance whether the firm earned
    # consensus on most/some/none of the gaps it covers, without
    # expanding the per-gap detail. Empty (n_attributed == 0) means
    # this partner came from legacy single-provider data — no chips
    # render in that case.
    n_consensus = 0
    n_gemini_only = 0
    n_claude_only = 0
    n_needs_review = 0
    for ge in p["gaps"]:
        cb = ge.get("confirmed_by") or []
        if len(cb) >= 2:
            n_consensus += 1
        elif "gemini" in cb:
            n_gemini_only += 1
        elif "claude" in cb:
            n_claude_only += 1
        if ge.get("needs_review"):
            n_needs_review += 1
    n_attributed = n_consensus + n_gemini_only + n_claude_only

    with ui.card().classes("w-full"):
        with ui.row().classes("items-start w-full gap-3"):
            with ui.column().classes("gap-0 flex-1"):
                # Clickable name -> profile dialog
                ui.label(p["name"]).classes(
                    "text-lg font-semibold text-primary cursor-pointer hover:underline"
                ).on(
                    "click",
                    lambda partner=p["raw_partner"]: _open_partner_profile(partner),
                )
                bits = [f"Solves {len(p['gaps'])} gap{'s' if len(p['gaps']) != 1 else ''}"]
                if capability_focus:
                    bits.append(capability_focus)
                ui.label(" · ".join(bits)).classes("text-sm opacity-70")

                # Contact summary (compact one-liner)
                contact_bits: list[str] = []
                if contact.get("primary_location"):
                    contact_bits.append(contact["primary_location"])
                if contact.get("website"):
                    contact_bits.append(contact["website"])
                if contact.get("general_email"):
                    contact_bits.append(contact["general_email"])
                if contact_bits:
                    ui.label(" · ".join(contact_bits)).classes("text-xs opacity-60 font-mono")

                # Partner-level provenance summary chips. Renders only
                # when the dual pipeline produced attributable data on
                # at least one of this partner's gaps.
                if n_attributed:
                    with ui.row().classes("gap-1 pt-1 flex-wrap"):
                        if n_consensus:
                            ui.chip(
                                f"CONSENSUS · {n_consensus}",
                                icon="verified",
                            ).props("color=green-7 text-color=white size=sm").tooltip(
                                f"Both providers (Gemini + Claude+web) "
                                f"independently surfaced this partner "
                                f"for {n_consensus} of {len(p['gaps'])} "
                                f"gap(s). Confidence boosted on those rows."
                            )
                        if n_gemini_only:
                            ui.chip(
                                f"Gemini only · {n_gemini_only}",
                                icon="travel_explore",
                            ).props("color=blue-grey-6 text-color=white outline size=sm").tooltip(
                                f"Only Gemini grounded research surfaced "
                                f"this partner for {n_gemini_only} gap(s)."
                            )
                        if n_claude_only:
                            ui.chip(
                                f"Claude only · {n_claude_only}",
                                icon="travel_explore",
                            ).props("color=blue-grey-6 text-color=white outline size=sm").tooltip(
                                f"Only Claude+web research surfaced this partner for {n_claude_only} gap(s)."
                            )
                        if n_needs_review:
                            ui.chip(
                                f"Verify · {n_needs_review}",
                                icon="error_outline",
                            ).props("color=amber-7 text-color=white size=sm").tooltip(
                                f"{n_needs_review} gap row(s) are "
                                f"single-provider hits at sub-HIGH "
                                f"confidence — worth verifying before "
                                f"reaching out."
                            )

            ui.chip(label, icon=qicon).props(f"color={qcolor} text-color=white")

        # Gaps this partner could fill — default-open when we have
        # provenance to show, so the per-row chips are visible without
        # an extra click. Stays collapsed for legacy/library partners
        # where there's no new info inside.
        with ui.expansion(
            f"Gaps this partner could fill ({len(p['gaps'])})",
            icon="rule",
            value=bool(n_attributed),
        ).classes("w-full"):
            for ge in p["gaps"]:
                _icon, color, _, _ = _SEVERITY_VISUAL.get(ge["severity"], ("help_outline", "slate-6", "", ""))
                with ui.row().classes("items-start gap-2 w-full pl-2 py-1"):
                    ui.chip(ge["severity"].replace("_", " ")).props(f"color={color} text-color=white size=xs")
                    # Dual-pipeline provenance — surface which provider(s)
                    # surfaced this partner for THIS gap. Consensus =
                    # both Gemini + Claude+web independently agreed.
                    confirmed_by = ge.get("confirmed_by") or []
                    if len(confirmed_by) >= 2:
                        ui.chip("CONSENSUS", icon="verified").props(
                            "color=green-7 text-color=white size=xs"
                        ).tooltip(
                            "Both providers (Gemini + Claude+web) "
                            "independently surfaced this partner for "
                            "this gap — confidence boosted."
                        )
                    elif "gemini" in confirmed_by:
                        ui.chip("Gemini only", icon="travel_explore").props(
                            "color=blue-grey-6 text-color=white outline size=xs"
                        ).tooltip("Only Gemini grounded research surfaced this partner. Claude+web did not.")
                    elif "claude" in confirmed_by:
                        ui.chip("Claude only", icon="travel_explore").props(
                            "color=blue-grey-6 text-color=white outline size=xs"
                        ).tooltip("Only Claude+web research surfaced this partner. Gemini did not.")
                    if ge.get("needs_review"):
                        ui.chip("Verify", icon="error_outline").props(
                            "color=amber-7 text-color=white size=xs"
                        ).tooltip(
                            "Single-provider hit at < HIGH confidence. "
                            "Worth a quick verification before "
                            "contacting."
                        )
                    if ge["is_user_selected_partner"]:
                        ui.chip("YOUR CHOICE", icon="check_circle").props(
                            "color=primary text-color=white size=xs"
                        )
                    elif ge["is_recommended"]:
                        ui.chip("RECOMMENDED").props("color=primary text-color=white outline size=xs")
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(f"{ge['gap_id']}: {ge['req_text']}").classes("text-sm")
                        if ge.get("mitigation_approach"):
                            ui.label(f"via: {ge['mitigation_approach']}").classes("text-xs opacity-60")
                        if ge.get("fit_rationale"):
                            ui.label(ge["fit_rationale"]).classes("text-xs italic opacity-70")

                    # Quick-select: pin this partner to this gap from the matrix.
                    if not ge["is_user_selected_partner"]:

                        def select_partner(
                            pk=ge["gap_pk"],
                            idx=ge["mitigation_index"],
                            partner_name=p["name"],
                            gid=ge["gap_id"],
                        ):
                            _set_gap_resolution(
                                gap_pk=pk,
                                selected_index=idx,
                                selected_partner=partner_name,
                            )
                            ui.notify(f"{gid}: selected {partner_name}", type="positive")
                            ui.navigate.reload()

                        ui.button(
                            "Select for this gap",
                            icon="check",
                            on_click=select_partner,
                        ).props("flat dense size=xs color=primary")

        # Footer actions
        with ui.row().classes("w-full justify-end gap-2 pt-2"):
            ui.button(
                "Open full profile",
                icon="open_in_new",
                on_click=lambda partner=p["raw_partner"]: _open_partner_profile(partner),
            ).props("flat dense color=primary")
            if not p["from_library"]:
                capability_focus = p.get("capability_focus")
                agent_profile = p.get("profile")

                def add_to_lib(
                    name=p["name"],
                    focus=capability_focus,
                    rationale=(p["gaps"][0]["fit_rationale"] if p["gaps"] else ""),
                    agent_profile=agent_profile,
                ):
                    from app.core.teaming_partners import add_partner

                    result = add_partner(
                        name=name,
                        capability_focus=focus,
                        fit_rationale=rationale,
                        confirmed=False,
                        profile=agent_profile,
                    )
                    if result.get("added"):
                        ui.notify(
                            f"Added {name} to teaming_partners.json.",
                            type="positive",
                        )
                    else:
                        ui.notify(f"Not added: {result.get('reason')}", type="warning")

                ui.button(
                    "Add to library",
                    icon="library_add",
                    on_click=add_to_lib,
                ).props("flat dense color=primary")


def _open_partner_profile(partner: dict) -> None:
    """Modal dialog with the partner's full profile.

    Source priority:
      1. partner['profile'] from the Strategist's structured output (preferred)
      2. data/teaming_partners.json entry by name match (for library partners
         where the agent's profile is sparse)
      3. Fallback: just show name + fit_rationale + capability_focus
    """
    from app.core.teaming_partners import get_partners_list

    name = partner.get("name") or "(unnamed partner)"
    profile = partner.get("profile") or {}
    from_lib = partner.get("from_library", True)

    library_entry = None
    if from_lib:
        for entry in get_partners_list():
            if (entry.get("name") or "").strip().lower() == name.strip().lower():
                library_entry = entry
                break

    # Resolve each section: prefer agent profile, fall back to library entry.
    overview = profile.get("overview") or (library_entry.get("relationship_summary") if library_entry else "")
    why_project = profile.get("why_fits_this_project") or partner.get("fit_rationale", "")
    why_quadratic = profile.get("why_fits_quadratic") or ""
    capabilities = profile.get("key_capabilities") or (
        list(library_entry.get("core_capabilities", [])) if library_entry else []
    )
    certs = profile.get("certifications_set_asides") or (
        list(library_entry.get("certifications_set_asides", [])) if library_entry else []
    )
    geo = list(library_entry.get("geographic_presence", [])) if library_entry else []
    engagement = profile.get("typical_engagement_model") or ""

    # Contact: merge agent's contact with library entry's contact (library wins
    # for fields the user has manually curated; agent fills any gaps).
    agent_contact = profile.get("contact") or {}
    library_contact = (library_entry.get("contact") or {}) if library_entry else {}
    contact = {
        k: (library_contact.get(k) or agent_contact.get(k) or None)
        for k in ("website", "primary_location", "general_email", "linkedin", "notes")
    }
    has_contact = any(v for v in contact.values())

    # Status chip
    if not from_lib:
        chip = ("MARKET SUGGESTION", "blue-grey-6")
    elif partner.get("confirmed"):
        chip = ("CONFIRMED", "green-7")
    else:
        chip = ("PROSPECT", "amber-7")

    with ui.dialog() as dlg, ui.card().classes("w-full max-w-3xl"):
        with ui.row().classes("items-center w-full gap-3"):
            ui.label(name).classes("text-xl font-semibold flex-1")
            ui.chip(chip[0]).props(f"color={chip[1]} text-color=white size=sm")
        if partner.get("capability_focus"):
            ui.label(f"Capability focus: {partner['capability_focus']}").classes("text-sm opacity-70")

        def _section(title: str, body: str | None) -> None:
            if not (body and body.strip()):
                return
            ui.label(title).classes("text-xs font-medium opacity-70 pt-3")
            ui.label(body).classes("text-sm whitespace-pre-wrap")

        def _list_section(title: str, items: list) -> None:
            if not items:
                return
            ui.label(title).classes("text-xs font-medium opacity-70 pt-3")
            with ui.column().classes("gap-0 pl-2"):
                for it in items:
                    ui.label(f"• {it}").classes("text-sm")

        _section("Overview", overview)
        _section("Why this firm fits the project", why_project)
        _section("Why this firm fits with Quadratic", why_quadratic)
        _list_section("Key capabilities", capabilities)
        _list_section("Certifications / set-asides", certs)
        _list_section("Geographic presence", geo)
        _section("Typical engagement model", engagement)

        # Contact info — only render if we have anything.
        if has_contact:
            ui.label("Contact").classes("text-xs font-medium opacity-70 pt-3")
            with ui.column().classes("gap-1 pl-2"):
                if contact.get("website"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("language").classes("text-sm opacity-60")
                        ui.link(contact["website"], target=contact["website"], new_tab=True).classes(
                            "text-sm"
                        )
                if contact.get("primary_location"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("location_on").classes("text-sm opacity-60")
                        ui.label(contact["primary_location"]).classes("text-sm")
                if contact.get("general_email"):
                    email = contact["general_email"]
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("email").classes("text-sm opacity-60")
                        ui.link(email, target=f"mailto:{email}", new_tab=True).classes("text-sm")
                if contact.get("linkedin"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("link").classes("text-sm opacity-60")
                        ui.link("LinkedIn", target=contact["linkedin"], new_tab=True).classes("text-sm")
                if contact.get("notes"):
                    ui.label(f"Notes: {contact['notes']}").classes("text-xs italic opacity-70 pt-1")

        if not from_lib:
            ui.separator().classes("my-3")
            ui.label(
                "This partner isn't in your library yet. Use the 'Add to library' "
                "button on the gap card to start tracking them as a prospect."
            ).classes("text-xs opacity-60")

        with ui.row().classes("w-full justify-end pt-3"):
            ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


def _set_gap_resolution(
    *,
    gap_pk: int,
    selected_index=_UNSET,
    selected_partner=_UNSET,
    resolved=_UNSET,
    notes=_UNSET,
) -> None:
    """Persist user decisions on a gap. Pass _UNSET (default) to leave a field
    alone; pass None explicitly to clear it; pass a value to set it.

    If selected_index changes WITHOUT a paired selected_partner, the partner
    is auto-cleared (different mitigation = different partner suggestions).
    """
    fields = {}
    if selected_index is not _UNSET:
        fields["selected_index"] = selected_index
    if selected_partner is not _UNSET:
        fields["selected_partner"] = selected_partner
    if resolved is not _UNSET:
        fields["resolved"] = resolved
    if notes is not _UNSET:
        fields["notes"] = notes
    update_gap_resolution(gap_pk, **fields)


def _render_gaps_tab(
    proposal_id: int,
    gaps: list[dict],
    *,
    has_matrix: bool,
    on_state_change=None,
) -> None:
    """Gaps tab — Shortfall Strategist output, grouped by severity/category.

    Uses a refreshable inner so 'Choose this' / 'Mark resolved' updates in
    place without a full page reload. Filter state and gaps snapshot live in
    closure scope so they survive refresh.

    NOTE for the future Writer Team agent: GapAnalysis.selected_mitigation_index,
    GapAnalysis.resolved, and GapAnalysis.resolution_notes capture the user's
    decisions on each gap. The Writer Team should consume these to choose
    which mitigation language to draft into each section, and to skip
    sections for resolved gaps where the mitigation language is already
    finalized.
    """
    if not gaps:
        if not has_matrix:
            _empty_state(
                "No compliance matrix yet — run intake first.",
                icon="rule",
            )
            return
        _empty_state(
            "Shortfall analysis hasn't been run for this proposal yet. "
            "Click 'Run shortfall analysis' in the header above.",
            icon="warning",
        )
        return

    # Mutable closure state for refreshable rendering.
    state: dict = {
        "gaps": list(gaps),
        # Filters: severity (None = all), resolution ("all" | "unresolved" | "resolved").
        "filter_sev": None,
        # Per-severity expansion open/closed state. Persists across
        # render_inner.refresh() so resolving a gap doesn't collapse the
        # surrounding folder. Defaults: deal-breakers + technical open
        # (most actionable buckets); major + minor closed.
        "expansion_open": {
            "deal_breaker": True,
            "technical": True,
            "major": False,
            "minor": False,
        },
        "filter_res": "all",
    }

    def _reload_gaps_from_db() -> None:
        """Re-snapshot from DB after a user action so card-level fields update."""
        with SessionLocal() as db:
            rows = db.execute(
                select(GapAnalysis, ComplianceMatrixItem)
                .join(
                    ComplianceMatrixItem,
                    ComplianceMatrixItem.id == GapAnalysis.requirement_id_fk,
                )
                .where(
                    GapAnalysis.proposal_id == proposal_id,
                    ComplianceMatrixItem.status == "active",
                )
                .order_by(GapAnalysis.gap_severity, GapAnalysis.id)
            ).all()
            state["gaps"] = [
                {
                    "id": g.id,
                    "gap_id": g.gap_id,
                    "severity": g.gap_severity.value
                    if hasattr(g.gap_severity, "value")
                    else str(g.gap_severity),
                    "current_state": g.current_state or "",
                    "mitigation_options": g.mitigation_options_json or [],
                    "recommended_index": g.recommended_mitigation_index,
                    "selected_index": g.selected_mitigation_index,
                    "selected_partner": g.selected_partner_name,
                    "resolved": bool(g.resolved),
                    "resolution_notes": g.resolution_notes or "",
                    "req_id": req.requirement_id,
                    "req_text": req.requirement_text,
                    "req_type": req.requirement_type.value
                    if hasattr(req.requirement_type, "value")
                    else str(req.requirement_type),
                    "req_source": (
                        f"{req.source_doc}"
                        + (f" §{req.source_section}" if req.source_section else "")
                        + (f" p.{req.source_page}" if req.source_page else "")
                    ),
                }
                for g, req in rows
            ]

    @ui.refreshable
    def render_inner() -> None:
        all_gaps = state["gaps"]
        by_sev: dict[str, list[dict]] = {sev: [] for sev in _SEVERITY_ORDER}
        for g in all_gaps:
            by_sev.setdefault(g["severity"], []).append(g)

        # A gap is "addressed" if the user has either explicitly marked it
        # resolved OR picked a mitigation. Selecting a path is itself a
        # decision; users don't always click "Mark resolved" separately.
        def _addressed(g: dict) -> bool:
            return bool(g["resolved"]) or g.get("selected_index") is not None

        n_resolved = sum(1 for g in all_gaps if _addressed(g))
        n_unresolved = len(all_gaps) - n_resolved
        active_sev = state["filter_sev"]
        active_res = state["filter_res"]

        def _set_filter_sev(sev: str | None) -> None:
            state["filter_sev"] = None if state["filter_sev"] == sev else sev
            render_inner.refresh()

        def _set_filter_res(res: str) -> None:
            state["filter_res"] = "all" if state["filter_res"] == res else res
            render_inner.refresh()

        # Summary chips — clickable filters. Active filter shows in solid color;
        # inactive shows outlined.
        with ui.row().classes("flex-wrap gap-2 pt-2"):

            def _chip(label: str, icon: str, count: int, *, color: str, is_active: bool, on_click) -> None:
                props = (
                    f"color={color} text-color=white clickable"
                    if is_active
                    else f"color={color} text-color={color} outline clickable"
                )
                ui.chip(label, icon=icon, on_click=on_click).props(props).classes("cursor-pointer")

            _chip(
                f"{len(all_gaps)} gaps total",
                "warning",
                len(all_gaps),
                color="primary",
                is_active=(active_sev is None and active_res == "all"),
                on_click=lambda: (
                    state.update(filter_sev=None, filter_res="all"),
                    render_inner.refresh(),
                ),
            )
            if n_unresolved:
                _chip(
                    f"{n_unresolved} unresolved",
                    "pending_actions",
                    n_unresolved,
                    color="blue-grey-6",
                    is_active=(active_res == "unresolved"),
                    on_click=lambda: _set_filter_res("unresolved"),
                )
            if n_resolved:
                _chip(
                    f"{n_resolved} resolved",
                    "check_circle",
                    n_resolved,
                    color="green-7",
                    is_active=(active_res == "resolved"),
                    on_click=lambda: _set_filter_res("resolved"),
                )
            for sev in _SEVERITY_ORDER:
                n = len(by_sev.get(sev, []))
                if not n:
                    continue
                _icon, color, _, _ = _SEVERITY_VISUAL[sev]
                label_text = "Technical" if sev == "technical" else sev.replace("_", " ")
                _chip(
                    f"{label_text}: {n}",
                    _icon,
                    n,
                    color=color,
                    is_active=(active_sev == sev),
                    on_click=lambda s=sev: _set_filter_sev(s),
                )

        # Filtered set
        def _passes(g: dict) -> bool:
            if active_sev and g["severity"] != active_sev:
                return False
            if active_res == "unresolved" and _addressed(g):
                return False
            if active_res == "resolved" and not _addressed(g):
                return False
            return True

        filtered_gaps = [g for g in all_gaps if _passes(g)]
        if not filtered_gaps:
            ui.label("No gaps match the current filter.").classes("text-sm opacity-60 py-4")
            return

        filtered_by_sev: dict[str, list[dict]] = {sev: [] for sev in _SEVERITY_ORDER}
        for g in filtered_gaps:
            filtered_by_sev.setdefault(g["severity"], []).append(g)

        # One expansion per severity. Open/closed state lives in
        # state["expansion_open"] so it survives render_inner.refresh()
        # — resolving a gap no longer collapses the surrounding folder.
        for sev in _SEVERITY_ORDER:
            items = filtered_by_sev.get(sev, [])
            if not items:
                continue
            icon, color, _, _ = _SEVERITY_VISUAL[sev]
            label = "Technical" if sev == "technical" else sev.replace("_", " ").title()
            title = f"{label}  ({len(items)})"

            def _remember_expansion(e, sev=sev) -> None:
                state["expansion_open"][sev] = bool(e.value)

            with ui.expansion(
                title,
                icon=icon,
                value=state["expansion_open"].get(sev, False),
                on_value_change=_remember_expansion,
            ).classes("w-full"):
                for g in items:
                    _render_gap_card(g, proposal_id=proposal_id, on_change=_after_change)

    def _after_change() -> None:
        _reload_gaps_from_db()
        render_inner.refresh()
        # Update the page chrome (tab badges + next-step banner) so it
        # reflects the new resolved/unresolved counts without a reload.
        if on_state_change is not None:
            on_state_change()

    # Framing panel — two strategic-posture controls that drive bulk
    # gap selection AND inject into the writer's cached prefix as an
    # APPROVED FRAMING block. Refreshable so the "Apply to N gaps"
    # preview count updates as the user toggles radios.
    @ui.refreshable
    def render_framing_panel() -> None:
        teaming_framing, build_framing = get_framing(proposal_id)

        n_unaddressed = sum(1 for g in state["gaps"] if not g["resolved"] and g["selected_index"] is None)
        n_would_apply = 0
        if teaming_framing or build_framing:
            for g in state["gaps"]:
                if g["resolved"] or g["selected_index"] is not None:
                    continue
                picked = pick_mitigation_for_framing(
                    g["mitigation_options"],
                    teaming_framing=teaming_framing,
                    build_framing=build_framing,
                    recommended_index=g["recommended_index"],
                )
                if picked is not None:
                    n_would_apply += 1

        with ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-400"):
            with ui.row().classes("items-center w-full gap-2"):
                ui.icon("psychology").classes("text-blue-700")
                ui.label("Framing").classes("text-base font-semibold")
                ui.element("div").classes("flex-1")
                if teaming_framing or build_framing:
                    ui.chip("ACTIVE", icon="check_circle").props("color=blue-7 text-color=white size=sm")
            ui.label(
                "Two strategic decisions that shape every gap response and "
                "every section draft. The writer reads these directly; click "
                "Apply to also bulk-select matching mitigations on the "
                "unaddressed gaps below."
            ).classes("text-xs opacity-70")

            with ui.row().classes("w-full gap-3 items-center pt-2"):
                ui.label("Teaming on this proposal?").classes("text-sm font-medium w-56")
                ui.radio(
                    {
                        "open": "Open to it",
                        "self_perform_only": "Self-perform only",
                        "": "Decide per gap",
                    },
                    value=(teaming_framing or ""),
                    on_change=lambda e: _on_teaming_change(e.value),
                ).props("inline dense")

            with ui.row().classes("w-full gap-3 items-center pt-1"):
                ui.label("How to fill capability gaps?").classes("text-sm font-medium w-56")
                ui.radio(
                    {
                        "custom_build_first": "Custom-build first",
                        "self_perform_first": "Self-perform first",
                        "": "Decide per gap",
                    },
                    value=(build_framing or ""),
                    on_change=lambda e: _on_build_change(e.value),
                ).props("inline dense")

            with ui.row().classes("items-center pt-3 gap-3"):
                btn_label = f"Apply to {n_would_apply} unaddressed gap{'s' if n_would_apply != 1 else ''}"
                apply_btn = ui.button(
                    btn_label,
                    icon="auto_fix_high",
                    on_click=_apply_framing,
                ).props("color=primary")
                if n_would_apply == 0:
                    apply_btn.props("disable").tooltip(
                        "Pick a framing answer above to enable bulk-select."
                        if not (teaming_framing or build_framing)
                        else "No unaddressed gaps match the current framing."
                    )
                ui.label(f"({n_unaddressed} unaddressed total)").classes("text-xs opacity-60")

    def _on_teaming_change(value) -> None:
        _, current_build = get_framing(proposal_id)
        set_framing(
            proposal_id,
            teaming_framing=value or None,
            build_framing=current_build,
        )
        render_framing_panel.refresh()

    def _on_build_change(value) -> None:
        current_teaming, _ = get_framing(proposal_id)
        set_framing(
            proposal_id,
            teaming_framing=current_teaming,
            build_framing=value or None,
        )
        render_framing_panel.refresh()

    def _apply_framing() -> None:
        counts = apply_framing_to_unaddressed_gaps(proposal_id)
        if counts.get("reason"):
            ui.notify(
                f"No framing applied: {counts['reason']}",
                type="warning",
            )
            return
        parts = [f"Applied to {counts['applied']} gap{'s' if counts['applied'] != 1 else ''}"]
        if counts["no_match"]:
            parts.append(f"{counts['no_match']} couldn't be matched (only teaming options available)")
        if counts["skipped"]:
            parts.append(f"{counts['skipped']} already addressed")
        ui.notify(
            "; ".join(parts),
            type="positive",
            multi_line=True,
            timeout=5000,
        )
        _after_change()
        render_framing_panel.refresh()

    # Two views of the same gap set:
    #   "Per gap" — the default; user works each gap individually.
    #   "Teaming partners" — cross-cut by partner (matrix + cards) for
    #       the teaming-vs-self-perform decision. Used to be a top-level
    #       tab; lives here because it's a gap-resolution choice.
    with ui.tabs().classes("w-full") as gap_subtabs:
        ui.tab("Per gap", icon="warning")
        ui.tab("Teaming partners", icon="handshake")
    with ui.tab_panels(gap_subtabs, value="Per gap").classes("w-full"):
        with ui.tab_panel("Per gap"):
            render_framing_panel()
            render_inner()
        with ui.tab_panel("Teaming partners"):
            _render_teaming_strategy_tab(proposal_id, gaps)


def _render_gap_card(g: dict, *, proposal_id: int, on_change) -> None:
    """Card per gap: requirement, current state, mitigation options, user actions.

    `on_change` is called after any user action (Choose this, Mark resolved/
    unresolved) so the parent can refresh in place without a full page reload.
    `proposal_id` flows through so the notes-dialog can record the source
    proposal for any cross-RFP decisions the user remembers.
    """
    sev = g["severity"]
    icon, color, bg, border = _SEVERITY_VISUAL.get(
        sev, ("help_outline", "slate-6", "slate-50", "border-slate-300")
    )
    # Resolved cards get a faded look so unresolved items stand out.
    card_classes = f"w-full {bg} border-l-4 {border}"
    if g["resolved"]:
        card_classes += " opacity-60"

    with ui.card().classes(card_classes):
        # Header row: gap_id + req_id + severity + resolved chip
        with ui.row().classes("items-center gap-3 w-full"):
            ui.icon(icon).classes(f"text-{color}")
            ui.label(f"{g['gap_id']}  ·  {g['req_id']}").classes("text-sm font-mono opacity-70")
            ui.label(g["req_type"]).classes("text-xs opacity-60")
            ui.element("div").classes("flex-1")  # spacer
            if g["resolved"]:
                ui.chip("RESOLVED", icon="check_circle").props("color=green-7 text-color=white size=sm")

        ui.label(g["req_text"]).classes("text-sm font-medium pt-1")
        ui.label(f"Source: {g['req_source']}").classes("text-xs opacity-60")

        # Current state
        ui.separator().classes("my-2")
        ui.label("What Quadratic has now:").classes("text-xs font-medium opacity-70")
        ui.label(g["current_state"] or "(no current state recorded)").classes("text-sm")

        # Mitigation options
        options = g["mitigation_options"]
        if options:
            ui.separator().classes("my-2")
            with ui.row().classes("items-center w-full"):
                ui.label(f"Mitigation options ({len(options)}):").classes(
                    "text-xs font-medium opacity-70 flex-1"
                )
                if g["selected_index"] is not None:
                    sel_label = (
                        options[g["selected_index"]].get("approach", f"Option {g['selected_index'] + 1}")
                        if g["selected_index"] < len(options)
                        else "?"
                    )
                    ui.label(f"Selected: {sel_label}").classes("text-xs font-medium text-primary")
            # Once a mitigation is chosen, the body of every option collapses
            # by default — the user has decided, no need to re-read the
            # language. They can expand any option to peek if needed.
            gap_has_selection = g.get("selected_index") is not None
            for i, opt in enumerate(options):
                is_recommended = g.get("recommended_index") == i
                is_selected = g.get("selected_index") == i
                # Visual emphasis: selected > recommended > neither
                inner_classes = "w-full bg-white"
                if is_selected:
                    inner_classes += " border-l-4 border-primary ring-2 ring-blue-300"
                elif is_recommended:
                    inner_classes += " border-l-4 border-primary"
                with ui.card().classes(inner_classes):
                    with ui.row().classes("items-center gap-2 w-full"):
                        if is_selected:
                            ui.chip("YOUR CHOICE", icon="check_circle").props(
                                "color=primary text-color=white size=sm"
                            )
                        elif is_recommended:
                            ui.chip("RECOMMENDED").props("color=primary text-color=white outline size=sm")
                        ui.label(opt.get("approach") or f"Option {i + 1}").classes("text-sm font-medium")
                        # Compact warning indicator visible even when collapsed
                        if opt.get("additional_action_required"):
                            ui.icon("warning").classes("text-amber-700 text-sm").tooltip(
                                f"Action required: {opt['additional_action_required']}"
                            )
                        ui.element("div").classes("flex-1")
                        if is_selected:

                            def clear_selection(pk=g["id"]):
                                _set_gap_resolution(gap_pk=pk, selected_index=None)
                                ui.notify(
                                    f"{g['gap_id']}: cleared mitigation choice",
                                    type="positive",
                                )
                                on_change()

                            ui.button(
                                "Clear selection",
                                icon="close",
                                on_click=clear_selection,
                            ).props("flat dense size=sm color=blue-grey-7")
                        else:

                            def select_this(pk=g["id"], idx=i):
                                _set_gap_resolution(gap_pk=pk, selected_index=idx)
                                ui.notify(
                                    f"{g['gap_id']}: selected option {idx + 1}",
                                    type="positive",
                                )
                                on_change()

                            ui.button(
                                "Choose this",
                                icon="radio_button_unchecked",
                                on_click=select_this,
                            ).props("flat dense size=sm")

                    # Details section — collapsible. Forward-reference details_col
                    # in the toggle's closure: it's defined below before the
                    # button is ever clicked.
                    def make_toggle(initial_visible: bool, details=None, button=None):
                        state = {"visible": initial_visible}

                        def toggle():
                            state["visible"] = not state["visible"]
                            details.set_visibility(state["visible"])
                            button.text = "Hide details" if state["visible"] else "Show details"
                            button.icon = "unfold_less" if state["visible"] else "unfold_more"

                        return toggle

                    initial_visible = not gap_has_selection
                    details_col = ui.column().classes("w-full")
                    toggle_btn = ui.button(
                        "Hide details" if initial_visible else "Show details",
                        icon="unfold_less" if initial_visible else "unfold_more",
                    ).props("flat dense size=xs color=blue-grey-7 align=left")
                    toggle_btn.on("click", make_toggle(initial_visible, details_col, toggle_btn))

                    with details_col:
                        if opt.get("proposal_language_draft"):
                            ui.label("Proposal language:").classes("text-xs opacity-60 pt-1")
                            ui.label(opt["proposal_language_draft"]).classes(
                                "text-sm italic whitespace-pre-wrap"
                            )
                        if opt.get("honesty_check"):
                            ui.label("Honesty check:").classes("text-xs opacity-60 pt-1")
                            ui.label(opt["honesty_check"]).classes("text-xs")
                        if opt.get("additional_action_required"):
                            with ui.row().classes("items-center gap-1 pt-1"):
                                ui.icon("warning").classes("text-amber-700")
                                ui.label(f"Action required: {opt['additional_action_required']}").classes(
                                    "text-xs text-amber-700 font-medium"
                                )
                        # Partner suggestions for teaming approaches
                        partners = opt.get("partner_suggestions") or []
                        if partners:
                            ui.label("Suggested partners (click name for full profile):").classes(
                                "text-xs opacity-60 pt-1"
                            )
                            for p in partners:
                                from_lib = p.get("from_library", True)
                                confirmed = p.get("confirmed")
                                if not from_lib:
                                    badge = ("MARKET SUGGESTION", "blue-grey-6")
                                elif confirmed:
                                    badge = ("CONFIRMED", "green-7")
                                else:
                                    badge = ("PROSPECT", "amber-7")

                                partner_selected = g.get("selected_partner") == p.get("name") and is_selected
                                row_classes = "items-start gap-2 pl-2 w-full py-1 rounded"
                                if partner_selected:
                                    row_classes += " bg-blue-50 ring-2 ring-blue-300"

                                with ui.row().classes(row_classes):
                                    ui.chip(badge[0]).props(f"color={badge[1]} text-color=white size=xs")
                                    with ui.column().classes("gap-0 flex-1"):
                                        name_text = p.get("name", "?")
                                        ui.label(name_text).classes(
                                            "text-sm font-medium text-primary cursor-pointer hover:underline"
                                        ).on(
                                            "click",
                                            lambda partner=p: _open_partner_profile(partner),
                                        )
                                        if p.get("fit_rationale"):
                                            ui.label(p["fit_rationale"]).classes("text-xs opacity-70")
                                    if partner_selected:
                                        ui.chip("SELECTED", icon="check_circle").props(
                                            "color=primary text-color=white size=xs"
                                        )
                                    else:
                                        partner_name = p.get("name", "")

                                        def select_partner(
                                            pk=g["id"],
                                            idx=i,
                                            partner_name=partner_name,
                                        ):
                                            _set_gap_resolution(
                                                gap_pk=pk,
                                                selected_index=idx,
                                                selected_partner=partner_name,
                                            )
                                            ui.notify(
                                                f"{g['gap_id']}: selected {partner_name}",
                                                type="positive",
                                            )
                                            on_change()

                                        ui.button(
                                            "Select",
                                            icon="check",
                                            on_click=select_partner,
                                        ).props("flat dense size=xs color=primary")
                                    if not from_lib:
                                        partner_name = p.get("name", "")
                                        capability_focus = p.get("capability_focus")
                                        fit_rationale = p.get("fit_rationale", "")
                                        agent_profile = p.get("profile")

                                        def add_to_lib(
                                            name=partner_name,
                                            focus=capability_focus,
                                            rationale=fit_rationale,
                                            agent_profile=agent_profile,
                                        ):
                                            from app.core.teaming_partners import add_partner

                                            result = add_partner(
                                                name=name,
                                                capability_focus=focus,
                                                fit_rationale=rationale,
                                                confirmed=False,
                                                profile=agent_profile,
                                            )
                                            if result.get("added"):
                                                ui.notify(
                                                    f"Added {name} to teaming_partners.json "
                                                    "(with contact info). Re-run shortfall to "
                                                    "see it as 'PROSPECT'.",
                                                    type="positive",
                                                    multi_line=True,
                                                    timeout=6000,
                                                )
                                            else:
                                                ui.notify(
                                                    f"Not added: {result.get('reason')}",
                                                    type="warning",
                                                )

                                        ui.button(
                                            "Add to library",
                                            icon="library_add",
                                            on_click=add_to_lib,
                                        ).props("flat dense size=xs color=primary")
                    # Apply initial visibility AFTER the body is populated.
                    details_col.set_visibility(initial_visible)
        else:
            # No mitigations => either nothing to fix, or no_bid recommended
            ui.separator().classes("my-2")
            ui.label("No mitigation options proposed — Strategist may have recommended no-bid.").classes(
                "text-xs italic opacity-70"
            )

        # Resolution actions
        ui.separator().classes("my-2")
        with ui.row().classes("items-center gap-2 w-full"):

            def toggle_resolved(pk=g["id"], current=g["resolved"]):
                _set_gap_resolution(gap_pk=pk, resolved=not current)
                ui.notify(
                    f"{g['gap_id']}: {'unresolved' if current else 'resolved'}",
                    type="positive",
                )
                on_change()

            if g["resolved"]:
                ui.button("Mark unresolved", icon="undo", on_click=toggle_resolved).props(
                    "flat dense color=blue-grey-7"
                )
            else:
                ui.button("Mark resolved", icon="task_alt", on_click=toggle_resolved).props(
                    "flat dense color=green-7"
                )

            def open_notes_editor(
                pk=g["id"],
                gap_id=g["gap_id"],
                existing=g["resolution_notes"],
                req_text=g["req_text"],
            ):
                # Pre-fill suggested topic from the requirement text (truncated).
                suggested_topic = (req_text or "")[:80].rstrip()
                if len(req_text or "") > 80:
                    suggested_topic = suggested_topic.rstrip(",.;:") + "…"

                with ui.dialog() as dlg, ui.card().classes("w-full max-w-xl"):
                    ui.label(f"Resolution notes — {gap_id}").classes("text-base font-medium")
                    ui.label(
                        "Why is this resolved or how is it being addressed? "
                        "Notes will be available to the Writer Team and persist "
                        "with this proposal."
                    ).classes("text-xs opacity-60")
                    ta = (
                        ui.textarea(label="Notes", value=existing or "")
                        .classes("w-full")
                        .props("autofocus rows=5")
                    )

                    # Cross-RFP memory capture
                    ui.separator().classes("my-2")
                    remember_cb = ui.checkbox(
                        "Remember this decision for future similar gaps "
                        "(adds to Quadratic's cross-RFP decisions ledger)",
                        value=False,
                    )
                    topic_input = ui.input(
                        label="Topic (short name for the decision)",
                        value=suggested_topic,
                        placeholder="e.g., NC eProcurement Sourcing Tool registration",
                    ).classes("w-full")
                    topic_input.bind_visibility_from(remember_cb, "value")
                    ui.label(
                        "When checked, the Shortfall Strategist will see this "
                        "decision on every future RFP and apply it to similar gaps."
                    ).classes("text-xs opacity-60").bind_visibility_from(remember_cb, "value")

                    with ui.row().classes("w-full justify-end gap-2 pt-2"):
                        ui.button("Cancel", on_click=dlg.close).props("flat")

                        def clear_notes() -> None:
                            _set_gap_resolution(gap_pk=pk, notes=None)
                            ui.notify("Notes cleared.", type="positive")
                            dlg.close()
                            on_change()

                        def save_notes() -> None:
                            text = (ta.value or "").strip() or None
                            saved_decision = None
                            if remember_cb.value:
                                topic = (topic_input.value or "").strip()
                                if not topic:
                                    ui.notify(
                                        "Topic is required to remember the decision.",
                                        type="warning",
                                    )
                                    return
                                if not text:
                                    ui.notify(
                                        "Notes are required to remember the decision.",
                                        type="warning",
                                    )
                                    return

                            # Validate the optional cross-RFP capture before
                            # mutating proposal-local notes. A failed topic or
                            # empty-note validation must leave both stores
                            # unchanged while the dialog remains open.
                            _set_gap_resolution(gap_pk=pk, notes=text)

                            if remember_cb.value:
                                from app.core.decisions import add_decision

                                result = add_decision(
                                    topic=topic,
                                    decision=text,
                                    applies_to_gaps_like=req_text,
                                    source_proposal_id=proposal_id,
                                    source_gap_id=gap_id,
                                )
                                if result.get("added"):
                                    saved_decision = result["decision"]["id"]
                                else:
                                    ui.notify(
                                        f"Decision NOT added: {result.get('reason')}. Notes were saved.",
                                        type="warning",
                                        multi_line=True,
                                    )

                            if saved_decision:
                                ui.notify(
                                    f"Notes saved + remembered as {saved_decision}. "
                                    "Future shortfall runs will see it.",
                                    type="positive",
                                    multi_line=True,
                                    timeout=6000,
                                )
                            else:
                                ui.notify("Notes saved.", type="positive")
                            dlg.close()
                            on_change()

                        if existing:
                            ui.button("Clear", icon="delete_outline", on_click=clear_notes).props(
                                "flat color=negative"
                            )
                        ui.button("Save", icon="save", on_click=save_notes).props("color=primary")
                dlg.open()

            note_label = "Edit notes" if g["resolution_notes"] else "Add notes"
            note_icon = "edit_note" if g["resolution_notes"] else "note_add"
            ui.button(note_label, icon=note_icon, on_click=open_notes_editor).props(
                "flat dense color=blue-grey-7"
            )

            if g["resolution_notes"]:
                ui.label(f"Notes: {g['resolution_notes']}").classes("text-xs italic opacity-70 flex-1")
