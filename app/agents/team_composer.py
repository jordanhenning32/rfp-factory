"""Team Composer — proposes a delivery team roster (roles + labor
categories + time allocations + phase coverage) from RFP scope.

User clicks 'Propose Team (AI)' on the Team tab. The agent reads
the outline, compliance matrix, Quadratic profile, and (when
available) the cost analyst's phase definitions. It returns a
list of proposed roles. The user reviews each row in a preview
dialog, then on Apply the roster is replaced. The user then
assigns specific people to each role via the Add/Edit dialog.

The agent does NOT pick named people — that's the user's call.
Every proposed role comes with assigned_person blank; person_kind
defaults to 'named' so the user can pick someone from the
profile/KB dropdown when they fill it in.

Single Sonnet 4.6 tool call. ~$0.05-0.15 depending on prompt size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import get_settings
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


@dataclass
class ProposedRole:
    """One proposed role from the Team Composer."""

    role_name: str
    labor_category: str
    time_allocation_pct: int
    phases_active: list[str] = field(default_factory=list)
    bio_summary: str = ""  # role description (1-2 sentences)
    rationale: str = ""  # why this role at this allocation
    # — preview-only, not persisted


@dataclass
class TeamCompositionResult:
    roles: list[ProposedRole] = field(default_factory=list)
    summary: str = ""  # overall team narrative (2-3 sentences)


_TOOL: dict = {
    "name": "report_proposed_team",
    "description": (
        "Propose a delivery team for the RFP. Output the team "
        "summary (2-3 sentences explaining the staffing model) "
        "plus 4-8 roles. Each role is a position, NOT a person — "
        "the user assigns specific people afterward. Use Quadratic's "
        "GSA OLM labor categories from the catalog provided. Be "
        "realistic about team size: under-staffed bids fail "
        "evaluator credibility, over-staffed bids price out."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 sentences. The team's overall staffing "
                    "model and how it covers the scope. Specific, "
                    "no marketing fluff."
                ),
            },
            "roles": {
                "type": "array",
                "description": (
                    "4-8 typical for a sub-$2M state IT services "
                    "engagement. More than 10 roles on a small bid "
                    "is over-built; refactor."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "role_name": {
                            "type": "string",
                            "description": (
                                "Short title (2-4 words). E.g., "
                                "'Project Manager', 'Solution "
                                "Architect', 'Lead Engineer'. NOT "
                                "a person's name."
                            ),
                        },
                        "labor_category": {
                            "type": "string",
                            "description": (
                                "Must match a 'title' from the "
                                "labor catalog provided in the user "
                                "prompt. The category drives wrap-"
                                "rate math downstream."
                            ),
                        },
                        "time_allocation_pct": {
                            "type": "integer",
                            "minimum": 5,
                            "maximum": 100,
                            "description": (
                                "Share of full-time effort over "
                                "the period of performance, "
                                "expressed as 0-100. PM on a 12-"
                                "month state IT engagement is "
                                "typically 40-60%; an SME often "
                                "10-25%; a full-time IC 80-100%."
                            ),
                        },
                        "phases_active": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Lifecycle phase identifiers this "
                                "role works in. When the user "
                                "prompt provides phase definitions, "
                                "use those exact labels (e.g., "
                                "'Phase 1: Discovery'). Otherwise "
                                "leave empty — the user fills in "
                                "later."
                            ),
                        },
                        "bio_summary": {
                            "type": "string",
                            "description": (
                                "1-2 sentence ROLE DESCRIPTION — "
                                "what this role does on this "
                                "engagement, not a personal bio. "
                                "User overwrites with the assigned "
                                "person's actual bio. Example: "
                                "'PMP-certified senior PM "
                                "responsible for governance, "
                                "stakeholder reporting, and "
                                "monthly evaluator reviews.'"
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "1-2 sentences explaining why this "
                                "role is needed and why the % "
                                "allocation is right. Cites "
                                "specific compliance items / "
                                "scope drivers. Shown to the user "
                                "in the preview dialog only — not "
                                "persisted to the roster."
                            ),
                        },
                    },
                    "required": [
                        "role_name",
                        "labor_category",
                        "time_allocation_pct",
                        "bio_summary",
                        "rationale",
                    ],
                },
            },
        },
        "required": ["roles", "summary"],
    },
}


_SYSTEM = """You are the Team Composer for Quadratic Digital — a small federal-services firm whose competitive edge is AI-accelerated delivery, deeper SME bench, and fit-to-purpose customization. Your one job is to propose a delivery team for an RFP: roles, labor categories, time allocations, and phase coverage. You do NOT pick named people — the user assigns specific staff to each role afterward.

DISCIPLINE:
- Cover the work. Walk the compliance matrix and outline; ask "who's delivering this? at what allocation?" Items required by the RFP that are not staffed are PROBLEMS. Common minimums for state IT services: 1 PM, 1-2 ICs (engineer/architect), 1 QA / Test, 1 SME or part-time technical lead.
- Be realistic about size. A $1M state-agency website CMS contract is a 3-5 person team for a 12-month PoP, not a 15-person enterprise integration. Read the scope; size accordingly.
- Match labor_category to ONE entry from the catalog you're shown. Categories not in the catalog get rejected by downstream tooling.
- Be transparent in rationale. "Senior Engineer III at 80% covers all Drupal development; lower seniority (II) is too junior for security-controlled state data" is the kind of line a federal CO can read and respect.
- Don't over-staff. Federal evaluators look for cost realism; bloated teams get scored down. If the scope is "build and host a CMS site", you don't need a Solutions Architect AND a DevSecOps Engineer AND a Senior Tech Lead.
- Don't under-staff. Bid teams that look unrealistically small ("two Software Engineer III's for a 12-month $1M contract — really?") fail evaluator credibility.

OUTPUT:
- 4-8 roles typically. More than 10 on a sub-$2M bid is over-built; refactor.
- Use ROLE NAMES, not personal names. "Project Manager", not "Alex Rivera".
- Set time_allocation_pct realistically for a 12-month PoP:
    * Project Manager (full lifecycle): 40-60%
    * Solution Architect / Tech Lead (peaks early, tapers): 30-50%
    * Senior Engineers (build phase heavy): 60-100%
    * Business Analyst (discovery + training peaks): 40-80%
    * Security/Compliance Lead (review + audit, not full-time): 15-30%
    * QA / Test Engineer (integration + UAT): 30-60%
    * SMEs (advisory, peaks at design and review milestones): 10-25%
- phases_active: when phase definitions are provided in the user prompt, list the EXACT phase identifiers from that prompt. Otherwise return an empty array — DO NOT invent phases.
- bio_summary: short ROLE DESCRIPTION (what this position does on this engagement). The user replaces it with the assigned person's bio when they fill in a name.
- rationale: explain why this role and this %. Cite specific compliance items or scope language when it drives the choice."""


_USER_TEMPLATE = """Propose a delivery team for this RFP.

=== RFP context ===
Title: {rfp_title}
Customer agency: {rfp_agency}
Period of performance: ~{pop_months} months

=== Scope summary (compliance matrix, labor-driving items) ===
{compliance_block}

=== Outline / section briefs ===
{outline_block}

=== Available labor catalog (Quadratic GSA OLM categories) ===
{labor_catalog_block}
{phases_block}
=== Quadratic context ===
{quadratic_summary}

Call report_proposed_team. Use ONLY labor_category values from the catalog above. Use the phase identifiers verbatim if any are listed. 4-8 roles typical."""


def propose_team(
    *,
    proposal_id: int,
    rfp_title: str,
    rfp_agency: str,
    pop_months: int,
    compliance_block: str,
    outline_block: str,
    labor_catalog_block: str,
    quadratic_summary: str,
    phases_block: str = "",
) -> TeamCompositionResult:
    """Run the team composer. Returns a TeamCompositionResult with
    the proposed roles + an overall team summary. Caller persists
    via app.services.team.replace_team after user approval.

    `phases_block` is optional — when the cost analyst has already
    run and produced phase_breakdown_json, the orchestrator passes
    the phase identifiers so the agent can map roles to phases.
    Empty string when no phases are defined yet."""
    settings = get_settings()
    user_prompt = _USER_TEMPLATE.format(
        rfp_title=rfp_title or "(unknown)",
        rfp_agency=rfp_agency or "(unknown)",
        pop_months=pop_months,
        compliance_block=compliance_block or "(no labor-driving compliance items)",
        outline_block=outline_block or "(no outline)",
        labor_catalog_block=labor_catalog_block or "(no labor catalog)",
        quadratic_summary=quadratic_summary or "(unknown)",
        phases_block=(
            f"\n=== Lifecycle phases (use these identifiers verbatim in phases_active) ===\n{phases_block}\n"
            if phases_block.strip()
            else ""
        ),
    )

    tool_input, usage = call_tool_for_model(
        model=settings.model_team_composer,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=8000,
        agent_name="team_composer",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") in ("max_tokens", "length"):
        n_partial = len(tool_input.get("roles") or [])
        raise RuntimeError(
            f"team_composer: output truncated at max_tokens "
            f"(in={usage['input_tokens']}, "
            f"out={usage['output_tokens']}). Got {n_partial} "
            f"partial role(s) before truncation."
        )

    roles: list[ProposedRole] = []
    for r in tool_input.get("roles") or []:
        try:
            pct_raw = r.get("time_allocation_pct")
            try:
                pct = int(round(float(pct_raw))) if pct_raw is not None else 0
            except (TypeError, ValueError):
                pct = 0
            pct = max(0, min(100, pct))
            roles.append(
                ProposedRole(
                    role_name=str(r.get("role_name") or "").strip() or "(unnamed role)",
                    labor_category=str(r.get("labor_category") or "").strip(),
                    time_allocation_pct=pct,
                    phases_active=[str(p).strip() for p in (r.get("phases_active") or []) if str(p).strip()],
                    bio_summary=str(r.get("bio_summary") or "").strip(),
                    rationale=str(r.get("rationale") or "").strip(),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "team_composer: skipping malformed role %r: %s",
                r,
                exc,
            )

    summary = str(tool_input.get("summary") or "").strip()

    log.info(
        "team_composer: proposal %d -> %d role(s) (%s)",
        proposal_id,
        len(roles),
        fmt_llm_usage(usage),
    )
    return TeamCompositionResult(roles=roles, summary=summary)


__all__ = [
    "ProposedRole",
    "TeamCompositionResult",
    "propose_team",
]
