"""Proposal team-roster service.

CRUD for proposal_team_members + the formatter that builds the
APPROVED TEAM ROSTER block injected into the Writer Team's cached
prefix. The block is what makes the writer stop emitting NEEDS_HUMAN
for staffing percentages and named personnel.

Phase 1A scope: manual entry only. Future phases add a Team Composer
agent that pre-seeds rows and a Cost Analyst inversion that consumes
this roster as input.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db.session import session_scope
from app.models import Proposal, ProposalTeamMember

log = logging.getLogger(__name__)


_VALID_PERSON_KINDS = ("named", "tbh", "sub")


# Job-title tokens that get stripped off the end of a personnel-doc
# filename when extracting a name. Resume filenames in the wild often
# include the role ("Robin Ellis Software Engineer.docx"), and that
# role bleeds into the Team-tab dropdown without this filter. Names
# never contain these tokens; the strip is suffix-only and aborts
# when it would leave fewer than 2 tokens, so an edge case like a
# person literally named "Mike Manager" is preserved.
_TITLE_TOKENS = frozenset(
    {
        # roles
        "engineer",
        "developer",
        "programmer",
        "architect",
        "analyst",
        "manager",
        "director",
        "scientist",
        "designer",
        "consultant",
        "specialist",
        "lead",
        "officer",
        "president",
        "coordinator",
        "administrator",
        "supervisor",
        "executive",
        "technician",
        "integrator",
        "planner",
        "owner",
        "researcher",
        "advisor",
        "strategist",
        "evangelist",
        "auditor",
        "accountant",
        "controller",
        "writer",
        # modifiers
        "senior",
        "sr",
        "junior",
        "jr",
        "principal",
        "staff",
        "chief",
        "associate",
        "assistant",
        "deputy",
        # domains
        "software",
        "systems",
        "system",
        "network",
        "security",
        "data",
        "cyber",
        "cybersecurity",
        "cloud",
        "devops",
        "qa",
        "business",
        "project",
        "product",
        "program",
        "technical",
        "marketing",
        "sales",
        "operations",
        "financial",
        "fiscal",
        "frontend",
        "backend",
        "fullstack",
        "infrastructure",
        "platform",
        "database",
        "web",
        "mobile",
        "ios",
        "android",
        "ai",
        "ml",
        # initialisms
        "vp",
        "cto",
        "ceo",
        "cfo",
        "cio",
        "ciso",
        "coo",
        "pm",
        "po",
        "sme",
    }
)


def _coerce_pct(value: Any) -> int:
    """Clamp 0..100; coerce strings/floats. Used everywhere the user
    can type a percent so a single helper enforces the bounds."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def get_team_members(proposal_id: int) -> list[dict]:
    """Read-only snapshot of the team for a proposal. Returns plain
    dicts so callers can use the data after the session closes."""
    with session_scope() as db:
        rows = (
            db.execute(
                select(ProposalTeamMember)
                .where(ProposalTeamMember.proposal_id == proposal_id)
                .order_by(
                    ProposalTeamMember.display_order,
                    ProposalTeamMember.id,
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": m.id,
                "role_name": m.role_name,
                "person_kind": m.person_kind,
                "assigned_person": m.assigned_person,
                "labor_category": m.labor_category,
                "wage_band": m.wage_band,
                "time_allocation_pct": m.time_allocation_pct,
                "experience_years": m.experience_years,
                "bio_summary": m.bio_summary,
                "phases_active": list(m.phases_active_json or []),
                "display_order": m.display_order,
            }
            for m in rows
        ]


def add_team_member(
    proposal_id: int,
    data: dict,
) -> int | None:
    """Insert a new team member row. Returns the new id, or None
    when the proposal doesn't exist. Display order auto-assigned to
    end-of-list."""
    role_name = (data.get("role_name") or "").strip()
    if not role_name:
        log.warning("add_team_member: empty role_name; skipping")
        return None

    kind = (data.get("person_kind") or "named").lower()
    if kind not in _VALID_PERSON_KINDS:
        kind = "named"

    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return None
        # Append to end of list — find the current max display_order.
        max_order = db.execute(
            select(ProposalTeamMember.display_order)
            .where(ProposalTeamMember.proposal_id == proposal_id)
            .order_by(ProposalTeamMember.display_order.desc())
            .limit(1)
        ).scalar_one_or_none()
        next_order = (max_order or 0) + 1

        m = ProposalTeamMember(
            proposal_id=proposal_id,
            role_name=role_name,
            person_kind=kind,
            assigned_person=(data.get("assigned_person") or "").strip() or None,
            labor_category=(data.get("labor_category") or "").strip() or None,
            wage_band=(data.get("wage_band") or "").strip() or None,
            time_allocation_pct=_coerce_pct(data.get("time_allocation_pct")),
            experience_years=(
                int(data["experience_years"]) if data.get("experience_years") not in (None, "") else None
            ),
            bio_summary=(data.get("bio_summary") or "").strip() or None,
            phases_active_json=list(data.get("phases_active") or []),
            display_order=next_order,
        )
        db.add(m)
        db.flush()
        new_id = m.id
        # New roster contents → user must re-approve. Clear the
        # approval timestamp so the UI gate re-asserts.
        prop.team_approved_at = None
    log.info(
        "add_team_member: proposal=%d role=%r kind=%s person=%r",
        proposal_id,
        role_name,
        kind,
        (data.get("assigned_person") or "")[:40],
    )
    return new_id


def update_team_member(member_id: int, data: dict) -> bool:
    """Update fields on an existing team member. Any key in `data`
    that maps to a model column is updated; missing keys are left
    alone. Clears the proposal's team_approved_at so the user
    re-affirms after edits."""
    with session_scope() as db:
        m = db.get(ProposalTeamMember, member_id)
        if m is None:
            return False
        if "role_name" in data:
            v = (data["role_name"] or "").strip()
            if v:
                m.role_name = v
        if "person_kind" in data:
            kind = (data["person_kind"] or "named").lower()
            if kind in _VALID_PERSON_KINDS:
                m.person_kind = kind
        if "assigned_person" in data:
            v = (data["assigned_person"] or "").strip()
            m.assigned_person = v or None
        if "labor_category" in data:
            v = (data["labor_category"] or "").strip()
            m.labor_category = v or None
        if "wage_band" in data:
            v = (data["wage_band"] or "").strip()
            m.wage_band = v or None
        if "time_allocation_pct" in data:
            m.time_allocation_pct = _coerce_pct(data["time_allocation_pct"])
        if "experience_years" in data:
            v = data["experience_years"]
            m.experience_years = int(v) if v not in (None, "") else None
        if "bio_summary" in data:
            v = (data["bio_summary"] or "").strip()
            m.bio_summary = v or None
        if "phases_active" in data:
            m.phases_active_json = list(data["phases_active"] or [])
        if "display_order" in data:
            try:
                m.display_order = int(data["display_order"])
            except (TypeError, ValueError):
                pass
        # Re-affirm required after any edit.
        prop = db.get(Proposal, m.proposal_id)
        if prop is not None:
            prop.team_approved_at = None
    return True


def replace_team(proposal_id: int, roles: list[dict]) -> int:
    """Atomically clear the proposal's existing team and write the
    given roles. Used by the Team Composer "Apply to Roster"
    action — the agent emits a fresh roster and the user accepts it,
    overwriting whatever was there. Returns the count of roles
    written. Clears proposal.team_approved_at so the user re-affirms
    before drafting picks up the new roster.

    `roles` is a list of dicts with role_name, person_kind,
    assigned_person, labor_category, wage_band, time_allocation_pct,
    experience_years, bio_summary, phases_active, display_order.
    Anything missing falls back to safe defaults.
    """
    written = 0
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return 0
        # Clear existing roster.
        db.query(ProposalTeamMember).filter(ProposalTeamMember.proposal_id == proposal_id).delete(
            synchronize_session=False
        )
        # Write new rows in caller-specified order; auto-assign
        # display_order from the iteration index when not provided.
        for idx, r in enumerate(roles):
            role_name = (r.get("role_name") or "").strip()
            if not role_name:
                continue
            kind = (r.get("person_kind") or "named").lower()
            if kind not in _VALID_PERSON_KINDS:
                kind = "named"
            try:
                display_order = int(r.get("display_order") if r.get("display_order") is not None else idx)
            except (TypeError, ValueError):
                display_order = idx
            db.add(
                ProposalTeamMember(
                    proposal_id=proposal_id,
                    role_name=role_name,
                    person_kind=kind,
                    assigned_person=((r.get("assigned_person") or "").strip() or None),
                    labor_category=((r.get("labor_category") or "").strip() or None),
                    wage_band=(r.get("wage_band") or "").strip() or None,
                    time_allocation_pct=_coerce_pct(
                        r.get("time_allocation_pct"),
                    ),
                    experience_years=(
                        int(r["experience_years"]) if r.get("experience_years") not in (None, "") else None
                    ),
                    bio_summary=((r.get("bio_summary") or "").strip() or None),
                    phases_active_json=list(r.get("phases_active") or []),
                    display_order=display_order,
                )
            )
            written += 1
        prop.team_approved_at = None
    log.info(
        "replace_team: proposal=%d wrote %d role(s)",
        proposal_id,
        written,
    )
    return written


def delete_team_member(member_id: int) -> bool:
    with session_scope() as db:
        m = db.get(ProposalTeamMember, member_id)
        if m is None:
            return False
        proposal_id = m.proposal_id
        db.delete(m)
        prop = db.get(Proposal, proposal_id)
        if prop is not None:
            prop.team_approved_at = None
    log.info("delete_team_member: id=%d", member_id)
    return True


def approve_team(proposal_id: int) -> bool:
    """Mark the team roster as user-approved. Sets
    proposal.team_approved_at to now. Returns False when:
      - proposal doesn't exist
      - the roster is empty
      - ANY member has kind != 'tbh' AND no assigned_person
        (the user must either assign a name or mark TBH)
    Validation also lives at the UI layer for a clean message;
    the server-side refusal here is defense in depth.

    Phase 2B: also advances proposal status from
    AWAITING_TEAM_APPROVAL → AWAITING_COST_BUILD when approval
    succeeds at that gate. Later statuses are left untouched —
    re-approving the team after the draft is written doesn't
    rewind the pipeline."""
    from app.core.enums import ProposalStatus

    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return False
        members = (
            db.execute(select(ProposalTeamMember).where(ProposalTeamMember.proposal_id == proposal_id))
            .scalars()
            .all()
        )
        if not members:
            return False
        for m in members:
            kind = (m.person_kind or "named").lower()
            if kind == "tbh":
                continue
            if not (m.assigned_person or "").strip():
                log.info(
                    "approve_team: refusing — role %r has no assigned person and is not marked TBH",
                    m.role_name,
                )
                return False
        prop.team_approved_at = datetime.utcnow()
        if prop.status == ProposalStatus.AWAITING_TEAM_APPROVAL:
            prop.status = ProposalStatus.AWAITING_COST_BUILD
            log.info(
                "approve_team: proposal=%d advanced AWAITING_TEAM_APPROVAL -> AWAITING_COST_BUILD",
                proposal_id,
            )
    log.info(
        "approve_team: proposal=%d (%d members)",
        proposal_id,
        len(members),
    )
    return True


def get_team_approval_state(proposal_id: int) -> dict:
    """Returns the team's approval state and what's blocking it.

    Shape:
      member_count: int — total members in the roster
      approved_at:  datetime | None — when the user last approved
      n_unfilled:   int — count of members missing a name (kind
                    in {'named', 'sub'} with empty assigned_person)
      unfilled_role_names: list[str] — role_name of each unfilled
                    member, for display in the Approve-button hint

    Used by the Team tab to gate the Approve button + surface the
    "X role(s) still need a name or 'To Be Hired'" message."""
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return {
                "member_count": 0,
                "approved_at": None,
                "n_unfilled": 0,
                "unfilled_role_names": [],
            }
        members = (
            db.execute(
                select(ProposalTeamMember)
                .where(ProposalTeamMember.proposal_id == proposal_id)
                .order_by(
                    ProposalTeamMember.display_order,
                    ProposalTeamMember.id,
                )
            )
            .scalars()
            .all()
        )
        unfilled: list[str] = []
        for m in members:
            kind = (m.person_kind or "named").lower()
            if kind == "tbh":
                continue
            if not (m.assigned_person or "").strip():
                unfilled.append(m.role_name)
        return {
            "member_count": len(members),
            "approved_at": prop.team_approved_at,
            "n_unfilled": len(unfilled),
            "unfilled_role_names": unfilled,
        }


def _normalize_wage_band(value: str | None) -> str:
    """Normalize a user-typed salary to the form internal_pricing_rules
    keys with — lowercase 'k' suffix. '170K' → '170k'. Empty / None
    passes through as empty string. The pricing service falls back
    to the wrap-rate formula for non-documented bands so this is
    cosmetic-cum-safety."""
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return s.lower()


def _default_wage_band_for_category(labor_category: str) -> str:
    """Look up the catalog's default_wage_band for a labor category
    so unapproved or partly-filled rosters (e.g. TBH roles where the
    user hasn't typed a salary yet) still produce parseable labor
    lines for the Cost Analyst. Empty string when the category isn't
    in the catalog — caller decides whether to skip or use a hard
    fallback.
    """
    if not labor_category:
        return ""
    try:
        from app.services.pricing import get_pricing_rules

        rules = get_pricing_rules()
        cat_lower = labor_category.strip().lower()
        for ll in rules.get("labor_catalog") or []:
            name = (ll.get("category") or ll.get("title") or "").strip().lower()
            if name == cat_lower:
                wb = (ll.get("default_wage_band") or "").strip()
                return wb.lower()
    except Exception:
        log.exception(
            "_default_wage_band_for_category: catalog read failed",
        )
    return ""


def roster_to_labor_lines(
    proposal_id: int,
    pop_months: int,
) -> list[dict]:
    """Translate the user-approved team roster into Cost-Analyst-
    shaped labor lines, deterministically. Returns empty list when
    the roster is unapproved OR empty (caller falls back to letting
    the agent decide labor mix).

    Hours per role = time_allocation_pct/100 × pop_months/12
                     × annual_billable_hours
    where annual_billable_hours comes from
    data/internal_pricing_rules.json. So 50% time on a 12-month
    PoP at 1880 billable hrs/yr = 940 hrs. A 25% sub on a 6-month
    PoP = 235 hrs.

    Returns dicts shaped to Cost Analyst's CostAnalystLaborLine:
        labor_category   from roster
        wage_band        normalized to lowercase ('170k')
        hours            float, computed
        rationale        synthesized from role + assigned_person
                         + experience + bio + person_kind
    """
    members = get_team_members(proposal_id)
    if not members:
        return []
    state = get_team_approval_state(proposal_id)
    if not state.get("approved_at"):
        # Unapproved roster — caller falls back to agent-decided labor.
        return []

    try:
        from app.services.pricing import get_pricing_rules

        rules = get_pricing_rules()
        annual_hours = float(rules.get("annual_billable_hours") or 1880)
    except Exception:
        annual_hours = 1880.0

    pop_fraction = max(0.0, float(pop_months) / 12.0)
    out: list[dict] = []
    for m in members:
        cat = (m.get("labor_category") or "").strip()
        if not cat:
            log.warning(
                "roster_to_labor_lines: skipping role %r — no labor_category set",
                m.get("role_name"),
            )
            continue
        pct = float(m.get("time_allocation_pct") or 0)
        hours = round((pct / 100.0) * pop_fraction * annual_hours, 1)
        if hours <= 0:
            log.warning(
                "roster_to_labor_lines: skipping role %r — 0 hours (pct=%s, pop_months=%s)",
                m.get("role_name"),
                pct,
                pop_months,
            )
            continue
        # Build the rationale string from whichever roster fields
        # are populated. Caller surfaces this on the Cost tab next
        # to the line and the Cost Volume Writer reads it for the
        # narrative, so be specific.
        bits: list[str] = []
        kind = (m.get("person_kind") or "named").lower()
        person = (m.get("assigned_person") or "").strip()
        role_name = (m.get("role_name") or "").strip() or "(unnamed role)"
        if kind == "tbh":
            person_phrase = "to-be-hired"
        elif kind == "sub":
            person_phrase = f"subcontractor: {person}" if person else "subcontractor"
        else:
            person_phrase = person or "(unassigned)"
        yrs = m.get("experience_years")
        yrs_phrase = f", {yrs} yrs exp" if yrs is not None else ""
        bits.append(
            f"{role_name} ({person_phrase}{yrs_phrase}); {int(round(pct))}% time over {pop_months}-month PoP."
        )
        bio = (m.get("bio_summary") or "").strip()
        if bio:
            bits.append(bio if bio.endswith(".") else bio + ".")
        rationale = " ".join(bits)
        # Wage band: use the user-typed salary when present; fall
        # back to the catalog's default_wage_band for that category
        # (covers TBH roles + members where the user hasn't filled
        # the salary yet). Without this fallback the Cost Analyst's
        # _parse_wage_band raises on empty strings and the whole run
        # fails — better to use a sensible default and let the user
        # override later.
        wage_band = _normalize_wage_band(m.get("wage_band"))
        if not wage_band:
            wage_band = _default_wage_band_for_category(cat)
            if wage_band:
                log.info(
                    "roster_to_labor_lines: role %r had empty "
                    "wage_band; using catalog default %r for "
                    "category %r",
                    m.get("role_name"),
                    wage_band,
                    cat,
                )
            else:
                log.warning(
                    "roster_to_labor_lines: role %r has empty "
                    "wage_band AND no catalog default for category "
                    "%r — skipping this line so the cost run "
                    "completes. Add a salary to surface it.",
                    m.get("role_name"),
                    cat,
                )
                continue
        out.append(
            {
                "labor_category": cat,
                "wage_band": wage_band,
                "hours": hours,
                "rationale": rationale,
            }
        )
    return out


def format_team_roster_for_cost_analyst(
    proposal_id: int,
    pop_months: int,
) -> str:
    """Render the approved roster as a prompt block the Cost Analyst
    consumes. Returns empty string when the roster is unapproved or
    empty — the agent then falls back to deciding labor mix as
    before. When non-empty, the agent is told to USE these
    categories, salaries, and hours verbatim; its remaining value-
    add is per-line rationale, phase allocations, ODCs, key risks,
    and the executive summary."""
    lines_data = roster_to_labor_lines(proposal_id, pop_months)
    if not lines_data:
        return ""
    members = get_team_members(proposal_id)
    member_lookup = {(m.get("labor_category") or "").strip(): m for m in members}
    block: list[str] = [
        "=== APPROVED TEAM ROSTER (USE THESE LABOR LINES VERBATIM) ===",
        "The user has explicitly approved this team composition. "
        "Your labor_lines output MUST match these categories, "
        "salaries, and hours EXACTLY — do not propose additional "
        "labor categories, do not adjust hours, do not substitute "
        "different salary bands. Reference these EXACT labor_category "
        "strings in your lifecycle_phases.labor_allocations as well. "
        "Your remaining judgment is per-line rationale (you can add "
        "to or refine what's shown here), phase allocations, ODCs, "
        "key_risks, and the executive_summary.",
        "",
    ]
    for ln in lines_data:
        cat = ln["labor_category"]
        member = member_lookup.get(cat) or {}
        person = (member.get("assigned_person") or "").strip()
        kind = (member.get("person_kind") or "named").lower()
        if kind == "tbh":
            person_phrase = "to-be-hired"
        elif kind == "sub":
            person_phrase = f"sub: {person}" if person else "subcontractor"
        else:
            person_phrase = person or "(unassigned)"
        block.append(f"  - role={member.get('role_name') or '?'} ({person_phrase})")
        block.append(f"      labor_category: {cat}")
        block.append(f"      wage_band: {ln['wage_band']}")
        block.append(
            f"      hours: {ln['hours']:.1f} "
            f"({int(round(float(member.get('time_allocation_pct') or 0)))}% "
            f"time over {pop_months}-month PoP)"
        )
        bio = (member.get("bio_summary") or "").strip()
        if bio:
            short_bio = bio if len(bio) <= 300 else bio[:297] + "..."
            block.append(f"      bio: {short_bio}")
    return "\n".join(block) + "\n"


def format_team_block_for_writer(proposal_id: int) -> str:
    """Render the APPROVED TEAM ROSTER block for the Writer Team's
    cached prefix. Empty string when the roster is empty OR the
    user hasn't approved it yet — in those cases the writer
    correctly defers to its prior NEEDS_HUMAN behavior.

    Format: one role per stanza, fielded so the writer can pick
    out specific facts (% time, salary, phase coverage)."""
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None or prop.team_approved_at is None:
            return ""
        rows = (
            db.execute(
                select(ProposalTeamMember)
                .where(ProposalTeamMember.proposal_id == proposal_id)
                .order_by(
                    ProposalTeamMember.display_order,
                    ProposalTeamMember.id,
                )
            )
            .scalars()
            .all()
        )

        if not rows:
            return ""

        lines: list[str] = ["=== APPROVED TEAM ROSTER ==="]
        lines.append(
            "User-approved on "
            f"{prop.team_approved_at:%Y-%m-%d %H:%M UTC}. "
            "Use these names, time allocations, and labor "
            "categories DIRECTLY in your prose. Do NOT emit "
            "[NEEDS_HUMAN] for staffing percentages, role "
            "names, or named personnel covered by this roster."
        )
        lines.append("")
        for m in rows:
            lines.append(f"Role: {m.role_name}")
            person = (m.assigned_person or "").strip()
            kind = (m.person_kind or "named").lower()
            kind_label = {
                "named": "named",
                "tbh": "to-be-hired",
                "sub": "subcontractor",
            }.get(kind, kind)
            yrs = f", {m.experience_years} yrs exp" if m.experience_years is not None else ""
            # TBH rows: don't echo the sentinel string back to the
            # writer (it'd come out as "Person: To Be Hired
            # (to-be-hired, 0 yrs exp)"). Use a clean canonical form.
            if kind == "tbh":
                lines.append("  Person: (to be hired)")
            else:
                if not person:
                    person = "(unassigned)"
                lines.append(f"  Person: {person} ({kind_label}{yrs})")
            if m.labor_category:
                lines.append(f"  Labor category: {m.labor_category}")
            if m.wage_band:
                lines.append(f"  Salary: {m.wage_band}")
            lines.append(
                f"  Time allocation: {m.time_allocation_pct}% of full-time over period of performance"
            )
            phases = list(m.phases_active_json or [])
            if phases:
                lines.append(f"  Active phases: {', '.join(str(p) for p in phases)}")
            if m.bio_summary:
                bio = m.bio_summary.strip()
                if len(bio) > 400:
                    bio = bio[:397] + "..."
                lines.append(f"  Bio: {bio}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _suggest_labor_category(
    role: str | None,
    years: int | None,
) -> str | None:
    """Map a key_personnel role string + years_experience to a
    labor_rate_card.categories title. Used by the Add Team Member
    dialog's autofill so the user gets a sensible labor_category
    pre-filled when they pick a person from the profile.

    Conservative — returns None when the role doesn't match any
    known keyword family. The user can always type a category by
    hand.
    """
    if not role:
        return None
    from app.core.company_profile import get_company_profile

    cats = (get_company_profile().get("labor_rate_card") or {}).get("categories") or []
    if not cats:
        return None

    # Keyword → category prefix in the rate card. Order matters:
    # check more-specific keywords first so "Senior Engineer" doesn't
    # match the generic "engineer" rule before the dedicated one.
    keyword_to_prefix = [
        ("ceo", "Program Director"),
        ("president", "Program Director"),
        ("chief", "Program Director"),
        ("program director", "Program Director"),
        ("project director", "Program Director"),
        ("project manager", "Project Manager"),
        ("pmo", "Project Manager"),
        ("requirements", "Business Analyst"),
        ("training", "Business Analyst"),
        ("business analyst", "Business Analyst"),
        ("iv&v", "Quality Assurance"),
        ("certification", "Quality Assurance"),
        ("qa", "Quality Assurance"),
        ("test", "Quality Assurance"),
        ("software engineer", "Software Engineer"),
        ("devsecops", "Software Engineer"),
        ("devops", "Software Engineer"),
        ("engineer", "Software Engineer"),
        ("developer", "Software Engineer"),
        ("architect", "Software Engineer"),
    ]
    role_lower = role.lower()
    matched_prefix: str | None = None
    for kw, prefix in keyword_to_prefix:
        if kw in role_lower:
            matched_prefix = prefix
            break
    if matched_prefix is None:
        return None

    yrs = int(years or 0)
    candidates = [c for c in cats if (c.get("title") or "").startswith(matched_prefix)]
    if not candidates:
        return None
    eligible = [c for c in candidates if int(c.get("min_years") or 0) <= yrs]
    if eligible:
        return max(
            eligible,
            key=lambda c: int(c.get("min_years") or 0),
        ).get("title")
    # Person under-qualifies for every level — return the lowest one
    # so the user at least gets a starting point.
    return min(
        candidates,
        key=lambda c: int(c.get("min_years") or 0),
    ).get("title")


def _extract_person_name_from_filename(filename: str) -> str | None:
    """Best-effort name extraction from a personnel-class KB
    document filename (resume / CV). Returns None when the cleanup
    yields nothing plausibly a name.

    Handles the variants we see in the wild:
      'Jane Doe Resume (2).docx'                    -> Jane Doe
      'AlexMorgan_Resume_2025_Present.docx'         -> Alex Morgan
      'CaseyReedResume2025-4.pdf'                   -> Casey Reed
      'D.Rivera - Resume - Current.pdf'             -> D Rivera
      'Resume - Taylor M Brooks - 10-24-25 (1).pdf' -> Taylor M Brooks
      'J-Nguyen-Resume_20251203.pdf'               -> J Nguyen
      'Sam_Parker_Resume (2).pdf'                   -> Sam Parker
      'Robin Ellis Software Engineer.docx'          -> Robin Ellis
      'Robin Ellis III.docx'                        -> Robin Ellis III

    Step 10 (job-title-token strip) drops trailing role / modifier /
    domain tokens — see _TITLE_TOKENS — so a filename that includes
    the person's title doesn't bleed into the assigned-person
    dropdown. The strip aborts when it would leave fewer than 2
    tokens, preserving edge cases like a person literally named
    'Mike Manager'. Roman-numeral generational suffixes (II / III /
    IV / VI / VII / VIII / IX) are preserved as uppercase by the
    title-case loop.
    """
    if not filename:
        return None
    import re

    s = filename
    # 1. Drop file extension
    s = re.sub(r"\.(pdf|docx?|rtf|txt)$", "", s, flags=re.IGNORECASE)
    # 2. Drop parenthesized fragments — version markers like "(2)",
    #    or "(Current)"
    s = re.sub(r"\([^)]*\)", "", s)
    # 3. Drop date strings BEFORE collapsing separators, since the
    #    date patterns rely on the original hyphens. Order: full
    #    8-digit YYYYMMDD, then short date forms.
    s = re.sub(r"\b(?:19|20)\d{6}\b", "", s)
    s = re.sub(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b", "", s)
    # 4. Underscores / hyphens / dots → spaces. After this step every
    #    token is whitespace-bordered, so subsequent word-boundary
    #    regexes work reliably (\bResume\b matches, where _Resume_
    #    earlier did not because '_' is a word char).
    s = re.sub(r"[_\-\.]+", " ", s)
    # 5. CamelCase split — insert space between lowercase→uppercase.
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    # 6. Letter↔digit split so 'Resume2025' becomes 'Resume 2025'.
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)
    s = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", s)
    # 7. Drop the literal Resume / CV / Present / Current tokens
    #    (all have proper word boundaries now after step 4).
    s = re.sub(r"\b(?:resume|cv)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(
        r"\b(?:present|current)\b",
        "",
        s,
        flags=re.IGNORECASE,
    )
    # 8. Drop YYYY years and any standalone digit groups
    s = re.sub(r"\b(?:19|20)\d{2}\b", "", s)
    s = re.sub(r"\b\d+\b", "", s)
    # 9. Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    # 10. Strip trailing job-title tokens — "Robin Ellis Software
    #     Engineer" -> "Robin Ellis". Walks tokens right-to-left,
    #     popping while the tail is a known title token AND removing
    #     it would still leave at least 2 tokens. The 2-token floor
    #     keeps over-broad cases like "Mike Manager" intact rather
    #     than collapsing to a first name.
    tokens = s.split(" ")
    while len(tokens) > 2 and tokens[-1].lower() in _TITLE_TOKENS:
        tokens.pop()
    s = " ".join(tokens)
    # Title-case each word, preserving 1-letter initials as uppercase
    # and Roman-numeral generational suffixes as all-caps (II / III /
    # IV etc. would otherwise get mangled to "Iii" by the naive
    # title-case rule).
    _ROMAN_SUFFIXES = {"II", "III", "IV", "VI", "VII", "VIII", "IX"}
    out_words: list[str] = []
    for w in s.split(" "):
        if not w:
            continue
        if len(w) == 1 and w.isalpha():
            out_words.append(w.upper())
        elif w.upper() in _ROMAN_SUFFIXES:
            out_words.append(w.upper())
        elif w[0].isalpha():
            out_words.append(w[0].upper() + w[1:].lower())
        else:
            out_words.append(w)
    out = " ".join(out_words)
    # Sanity: must contain a letter and not be absurdly long
    if not any(c.isalpha() for c in out) or len(out) > 60:
        return None
    return out


def _name_signature(name: str) -> tuple[str, str] | None:
    """First-letter-of-first-token + lowered last token. Used to
    fuzzy-dedupe resume-derived names against the canonical profile
    list (e.g., 'D Rivera' from a filename collapses onto the
    profile's 'David Rivera'). Returns None for single-word names —
    those are kept verbatim and only deduped on exact match."""
    parts = name.split()
    if len(parts) < 2:
        return None
    first_letter = parts[0][0].lower() if parts[0] else ""
    last = parts[-1].lower()
    if not first_letter or not last:
        return None
    return (first_letter, last)


def list_role_names() -> list[str]:
    """Distinct role names known to the system, for the Edit Team
    Member dialog's Role-name dropdown. Source: every distinct
    `role` value across `company_profile.key_personnel`. The dropdown
    is editable (with_input + add-unique) so this list is a
    suggestion, not a constraint — the user can type a new role any
    time. Deduped + alphabetized."""
    from app.core.company_profile import get_company_profile

    profile = get_company_profile()
    seen: set[str] = set()
    for entry in profile.get("key_personnel") or []:
        role = (entry.get("role") or "").strip()
        if role and role not in seen:
            seen.add(role)
    return sorted(seen)


def list_labor_categories() -> list[str]:
    """Distinct labor categories (GSA OLM titles) known to the system,
    for the Edit Team Member dialog's Labor-category dropdown. Pulls
    from BOTH canonical pricing sources because the codebase carries
    a known two-shape pricing data layout:
      - get_pricing_rules().labor_catalog uses `category`
      - company_profile.labor_rate_card.categories uses `title`
    Reading both means the dropdown surfaces the same options
    whichever source the user has more recently maintained. Deduped
    + alphabetized; editable in the dialog (with_input + add-unique)
    so unfamiliar RFP-driven titles can still be typed."""
    from app.core.company_profile import get_labor_rate_card
    from app.services.pricing import get_pricing_rules

    seen: set[str] = set()
    try:
        rules = get_pricing_rules()
        for ll in rules.get("labor_catalog") or []:
            # Two field-name shapes coexist (handoff documents this);
            # tolerate either to stay robust against rules-file drift.
            cat = (ll.get("category") or ll.get("title") or "").strip()
            if cat:
                seen.add(cat)
    except Exception:
        log.exception("list_labor_categories: pricing_rules read failed")

    try:
        rate_card = get_labor_rate_card() or {}
        for entry in rate_card.get("categories") or []:
            title = (entry.get("title") or "").strip()
            if title:
                seen.add(title)
    except Exception:
        log.exception("list_labor_categories: rate_card read failed")

    return sorted(seen)


def list_profile_person_names() -> dict[str, str]:
    """Available people for the team-roster dropdown, returned as
    a {value: display_label} dict. value == label everywhere — the
    dropdown shows bare names. (Role disambiguation for multi-role
    people lives in the secondary 'Profile role' dropdown that
    surfaces in the dialog when needed.)

    Sources:
      1. company_profile.key_personnel — canonical. Multiple
         entries for the same person under different roles
         (signature match: same first-initial + last-name) collapse
         onto ONE option whose value is the longest-name variant.
      2. Personnel-class KB documents (resumes / CVs) — names
         extracted from the filename. Deduped against profile
         entries by signature so a filename 'D.Rivera Resume.pdf'
         doesn't show up alongside the profile's 'David Rivera'.

    Caller is the Add Team Member dialog. Insertion order is
    alphabetized; the caller may prepend its own sentinel options
    (e.g., "To Be Hired") above this dict.
    """
    from sqlalchemy import select

    from app.core.company_profile import get_company_profile
    from app.db.session import session_scope
    from app.models.kb import KnowledgeBaseDocument

    seen_lower: set[str] = set()
    profile_signatures: set[tuple[str, str]] = set()
    entries: list[tuple[str, str]] = []  # (canonical_name, label)

    # Group profile entries by (first-initial, last-name) signature
    # so the same person under multiple roles collapses onto ONE
    # dropdown option whose label lists every role they can fill.
    # Entries without a signature (single-token names) get their
    # own bucket and are emitted individually.
    profile = get_company_profile()
    sig_groups: dict[tuple[str, str], list[dict]] = {}
    no_sig: list[dict] = []
    for entry in profile.get("key_personnel") or []:
        nm = (entry.get("name") or "").strip()
        if not nm:
            continue
        sig = _name_signature(nm)
        if sig is None:
            no_sig.append(entry)
        else:
            sig_groups.setdefault(sig, []).append(entry)

    for sig, group in sig_groups.items():
        merged = _merge_profile_entries(group)
        canonical = (merged.get("name") or "").strip()
        if not canonical:
            continue
        key = canonical.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        profile_signatures.add(sig)
        # Bare name in the dropdown — role disambiguation now lives
        # in the secondary "Profile role (controls autofill)"
        # dropdown that appears for multi-role people, so the
        # primary list stays clean.
        entries.append((canonical, canonical))

    for entry in no_sig:
        nm = (entry.get("name") or "").strip()
        if not nm:
            continue
        key = nm.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        entries.append((nm, nm))

    # KB resumes — query active personnel-class documents and merge.
    with session_scope() as db:
        rows = db.execute(
            select(KnowledgeBaseDocument.filename)
            .where(KnowledgeBaseDocument.document_class == "personnel")
            .where(KnowledgeBaseDocument.status == "active")
        ).all()
    for (filename,) in rows:
        candidate = _extract_person_name_from_filename(filename)
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen_lower:
            continue
        sig = _name_signature(candidate)
        if sig is not None and sig in profile_signatures:
            # Already represented under a canonical profile name.
            continue
        seen_lower.add(key)
        entries.append((candidate, candidate))

    entries.sort(key=lambda t: t[0].lower())
    return {nm: label for nm, label in entries}


def _merge_profile_entries(entries: list[dict]) -> dict:
    """Merge multiple company_profile.key_personnel entries that
    represent the SAME person under different roles (signature
    match: same first-initial + same last name).

    Picks the more-specific name (more tokens; tie → longer
    string) as canonical so middle-initial variants survive,
    unions all roles into 'role A / role B', combines past-
    performance lists deduped, and takes the max years_experience.
    Returns the same shape a single profile entry would have so
    the bio-synthesis logic in lookup_person_in_profile is shape-
    agnostic.
    """
    if not entries:
        return {}
    if len(entries) == 1:
        return dict(entries[0])
    canonical = max(
        entries,
        key=lambda e: (
            len((e.get("name") or "").split()),
            len(e.get("name") or ""),
        ),
    )
    roles: list[str] = []
    for e in entries:
        r = (e.get("role") or "").strip()
        if r and r not in roles:
            roles.append(r)
    focuses = [(e.get("focus") or "").strip() for e in entries]
    combined_focus = max(focuses, key=len) if any(focuses) else ""
    yrs_values = [e.get("years_experience") for e in entries if e.get("years_experience") is not None]
    combined_yrs = max(yrs_values) if yrs_values else None
    past_seen: list[str] = []
    for e in entries:
        for p in e.get("past_performance") or []:
            if p and p not in past_seen:
                past_seen.append(p)
    return {
        "name": canonical.get("name"),
        "role": " / ".join(roles) if roles else None,
        "focus": combined_focus or None,
        "years_experience": combined_yrs,
        "past_performance": past_seen,
    }


def _find_profile_entries_for_name(name: str) -> list[dict]:
    """Internal helper. Returns ALL key_personnel entries that
    match `name` either by exact case-insensitive equality OR by
    (first-initial, last-name) signature. This is the same matching
    logic the dropdown uses to collapse multi-role people, factored
    out so lookup_person_in_profile and get_person_roles_in_profile
    share it."""
    if not name or not name.strip():
        return []
    from app.core.company_profile import get_company_profile

    profile = get_company_profile()
    target = name.strip().lower()
    target_sig = _name_signature(name.strip())
    matching: list[dict] = []
    for entry in profile.get("key_personnel") or []:
        en = (entry.get("name") or "").strip()
        if not en:
            continue
        if en.lower() == target:
            matching.append(entry)
            continue
        if target_sig is not None and _name_signature(en) == target_sig:
            matching.append(entry)
    return matching


def get_person_roles_in_profile(name: str) -> list[str]:
    """Distinct role strings the named person fills, in profile
    order. Empty list when the person isn't in the profile.

    Used by the Add Team Member dialog: when this returns 2+ roles,
    the dialog surfaces a second dropdown so the user picks which
    profile role's data to autofill from. When 1 role (or none),
    the second dropdown stays hidden."""
    matching = _find_profile_entries_for_name(name)
    roles: list[str] = []
    for e in matching:
        r = (e.get("role") or "").strip()
        if r and r not in roles:
            roles.append(r)
    return roles


def lookup_person_in_profile(
    name: str,
    selected_role: str | None = None,
) -> dict | None:
    """Look up a person by name in company_profile.key_personnel.

    When `selected_role` is None, all matching entries (same name
    OR same first-initial + last-name signature) are merged — the
    bio combines roles, the focus picks the most descriptive,
    past_performance unions deduped, and years_experience takes
    the max.

    When `selected_role` is provided, the result is filtered down
    to the single profile entry whose role matches (case-
    insensitive), so the autofill bio + labor_category suggestion
    reflect THAT role only. Returns None when the role isn't
    among the person's profile entries.

    Used by the Add Team Member dialog's autofill — selected_role
    comes from the second 'Profile role' dropdown that surfaces
    when a person has more than one role.
    """
    matching = _find_profile_entries_for_name(name)
    if not matching:
        return None

    if selected_role:
        target_role = selected_role.strip().lower()
        filtered = [e for e in matching if (e.get("role") or "").strip().lower() == target_role]
        if not filtered:
            return None
        matching = filtered

    merged = _merge_profile_entries(matching)
    role = merged.get("role") or ""
    focus = merged.get("focus") or ""
    yrs = merged.get("years_experience")
    past = list(merged.get("past_performance") or [])

    if role and focus:
        bio_first = f"{role}, focused on {focus}"
    elif role:
        bio_first = role
    elif focus:
        bio_first = f"Focused on {focus}"
    else:
        bio_first = ""
    bio_second = ""
    if past:
        past_str = past[0]
        if len(past) > 1:
            past_str += f"; {past[1]}"
        bio_second = f"Past performance: {past_str}"
    bio_summary = bio_first
    if bio_first and bio_second:
        bio_summary = f"{bio_first}. {bio_second}"
    elif bio_second:
        bio_summary = bio_second
    if bio_summary and not bio_summary.endswith("."):
        bio_summary += "."

    return {
        "name": merged.get("name"),
        "role": role or None,
        "years_experience": yrs,
        "bio_summary": bio_summary or None,
        "suggested_labor_category": _suggest_labor_category(
            role,
            yrs,
        ),
    }


__all__ = [
    "add_team_member",
    "approve_team",
    "delete_team_member",
    "format_team_block_for_writer",
    "format_team_roster_for_cost_analyst",
    "get_person_roles_in_profile",
    "get_team_approval_state",
    "get_team_members",
    "list_profile_person_names",
    "lookup_person_in_profile",
    "replace_team",
    "roster_to_labor_lines",
    "update_team_member",
]
