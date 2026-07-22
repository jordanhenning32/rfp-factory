"""Needs Human Resolver — Phase B post-pass.

After the Writer Team drafts a section, the deterministic auto-
resolver in app.services.needs_human.auto_resolve_obvious_placeholders
handles the easy cases (signatures → CEO, doc dates → today).
Whatever [NEEDS_HUMAN] markers remain go through THIS agent, which
reads the cached proposal context (company profile, decisions
ledger, approved team roster, approved cost build, key personnel)
and decides — per marker — whether the answer is derivable.

Three actions per marker:
  edit    — the answer IS in the cached context; fill it in
            verbatim. Example: marker is 'Jamie Chen email',
            and key_personnel has it; resolver fills the email.
  reject  — the marker is a SUBMISSION CHECKLIST item that
            shouldn't appear in narrative ("verify buyer-portal
            registration before submission", "attach final hosting
            agreement"). Removing the marker leaves clean prose;
            the user tracks the actual verification on the
            Submission Checklist tab.
  skip    — the answer genuinely needs human judgment (partner
            confirmations not in the profile, named individual
            quotes/testimonials, externally-verified compliance
            attestations, anything outside the cached context).

Discipline: when in doubt, SKIP. A wrong autofill is worse than a
prompt the user has to action — false positives waste editor time
and risk submitting an incorrect claim.

Single Sonnet 4.6 tool call. ~$0.02-0.05 per section. Returns the
list of resolutions; the caller (auto_resolve_via_llm in
app.services.needs_human) applies them via the existing
resolve_placeholder service so the same draft-rewrite + reconcile
logic kicks in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import get_settings
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


_VALID_ACTIONS = ("edit", "reject", "skip")


@dataclass
class ResolverDecision:
    """One resolution decision from the Needs Human Resolver."""

    marker_text: str
    action: str  # "edit" | "reject" | "skip"
    value: str = ""
    reason: str = ""


@dataclass
class ResolverResult:
    decisions: list[ResolverDecision] = field(default_factory=list)


_TOOL: dict = {
    "name": "report_resolutions",
    "description": (
        "Decide per-placeholder whether the answer is derivable "
        "from the cached proposal context. Be CONSERVATIVE — "
        "'skip' is the safe default when the cached context "
        "doesn't unambiguously support a value. False autofills "
        "waste the user's editor time and risk submitting incorrect "
        "claims."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "description": (
                    "One entry per input placeholder. The "
                    "marker_text MUST exactly match one of the "
                    "input placeholders' marker_text values "
                    "(verbatim — the orchestrator does literal "
                    "string match to apply the resolution)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "marker_text": {
                            "type": "string",
                            "description": ("Exact marker_text from the input list. Required."),
                        },
                        "action": {
                            "type": "string",
                            "enum": list(_VALID_ACTIONS),
                            "description": (
                                "edit = answer is in cached "
                                "context; fill it in. "
                                "reject = pre-flight / submission-"
                                "checklist item that doesn't "
                                "belong in narrative; remove the "
                                "marker. "
                                "skip = needs human judgment; "
                                "leave for the user."
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": (
                                "For action='edit': the inline "
                                "replacement text. Concrete and "
                                "defensible — must be sourced "
                                "from the cached context, not "
                                "invented. Empty string for "
                                "action in {skip, reject}."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "1 sentence explaining the "
                                "decision. For 'edit', cite which "
                                "context source supplied the "
                                "value (e.g., 'from "
                                "company_profile.key_personnel'). "
                                "For 'reject', say what type of "
                                "checklist item it is. For "
                                "'skip', say what's missing from "
                                "the context."
                            ),
                        },
                    },
                    "required": ["marker_text", "action", "reason"],
                },
            },
        },
        "required": ["decisions"],
    },
}


_SYSTEM = """You are the Needs Human Resolver. After the Writer Team drafts a proposal section, you receive (a) the list of unresolved [NEEDS_HUMAN] placeholders the writer emitted, (b) the cached proposal context (company profile, decisions ledger, approved team roster, approved cost build, key personnel summary). Your one job is to decide, per placeholder, whether the answer is derivable.

THREE ACTIONS:
- edit: the answer IS in the cached context. Fill it in with a concrete value sourced verbatim from the context. The orchestrator will replace the [NEEDS_HUMAN] marker in the draft with your value.
- reject: the marker is a SUBMISSION CHECKLIST item that doesn't belong in narrative — pre-flight verifications ('verify buyer-portal registration before submission'), final-package attachments ('attach hosting agreement appendix'), executed-teaming confirmations that shouldn't appear inline, etc. Removing the marker leaves clean prose; the user tracks the actual verification on the Submission Checklist tab.
- skip: the answer genuinely needs human judgment. Default to this when:
    * The cached context doesn't unambiguously supply a value
    * The marker asks for a customer-specific quote / testimonial / endorsement
    * The marker asks for a partner confirmation not in the profile
    * The marker asks for an externally-verified compliance attestation (FedRAMP authorization status, third-party assessment results, named individual quotes)
    * You're not sure — better to leave for the user than guess

CRITICAL DISCIPLINE — false positives are MUCH worse than false negatives:
- A wrong autofill ("FedRAMP High authorized" when Quadratic isn't) puts a misleading claim in the proposal and risks a debarment finding.
- A 'skip' just leaves a placeholder for the user to action. Cheap to leave.
- When the context is ambiguous, ALWAYS skip.

EXAMPLES:

  Marker: "Jamie Chen email"
  Context: company_profile.key_personnel has Jamie Chen with no email field.
  Decision: action=skip, reason="key_personnel entry for Jamie Chen has no email field; user must supply".

  Marker: "Jamie Chen email"
  Context: key_personnel has Jamie Chen with email=jamie.chen@example.invalid.
  Decision: action=edit, value="jamie.chen@example.invalid", reason="from company_profile.key_personnel.email".

  Marker: "verify buyer-portal registration before submission"
  Context: any.
  Decision: action=reject, reason="pre-flight registration check belongs on Submission Checklist, not narrative".

  Marker: "confirm specialist engagement (named partner or individual) before submission"
  Context: profile has no relevant specialist named.
  Decision: action=skip, reason="profile does not name the required specialist; partner / individual must be confirmed by the user".

  Marker: "year-1 / year-3 / year-5 concurrent-user projections"
  Context: cost build doesn't have user projections; RFP excerpt would but isn't in cached context.
  Decision: action=skip, reason="user-projection numbers are not in cached context; user must supply from RFP analysis".

  Marker: "attach final hosting/license/support agreement appendix prior to submission"
  Context: any.
  Decision: action=reject, reason="package-attachment item belongs on Submission Checklist, not narrative".

  Marker: "% time PM"
  Context: approved team roster has Project Manager at 50% time.
  Decision: action=edit, value="50% of full-time over the period of performance", reason="from approved team roster's Project Manager allocation".

  Marker: "cloud-hosting reseller margin and license assumptions"
  Context: cost build has 'Cloud hosting: $60,000/year' as an ODC; nothing about reseller margin or licensing.
  Decision: action=skip, reason="cost build has the hosting amount but neither reseller margin nor licensing assumptions; user must supply".

OUTPUT — call report_resolutions with one decision per input marker. Every input marker_text MUST appear in your output exactly once. Match the input strings verbatim — the orchestrator does literal string match."""


_USER_TEMPLATE = """Resolve the [NEEDS_HUMAN] placeholders below where the cached context unambiguously supports a value. Skip when in doubt.

=== SECTION CONTEXT ===
section_id: {section_id}
section_title: {section_title}

=== UNRESOLVED PLACEHOLDERS ({n_markers} total) ===
{placeholders_block}

=== CACHED CONTEXT ===

--- Company profile (canonical) ---
{profile_summary}

--- Past decisions ledger ---
{decisions_text}

--- Approved team roster ---
{team_roster_block}

--- Approved cost build ---
{cost_build_block}

Call report_resolutions now. One decision per input marker. Match marker_text verbatim."""


def resolve_placeholders(
    *,
    proposal_id: int,
    section_id: str,
    section_title: str,
    placeholders: list[dict],
    profile_summary: str,
    decisions_text: str,
    team_roster_block: str,
    cost_build_block: str,
) -> ResolverResult:
    """Run the resolver. `placeholders` is a list of dicts with at
    least marker_text + category. Returns a ResolverResult; caller
    applies decisions via app.services.needs_human.resolve_placeholder.

    Returns an empty result when there are no placeholders to act on
    (no LLM call made)."""
    if not placeholders:
        return ResolverResult(decisions=[])

    settings = get_settings()
    placeholder_lines: list[str] = []
    for i, ph in enumerate(placeholders, 1):
        marker = (ph.get("marker_text") or "").strip()
        if not marker:
            continue
        category = (ph.get("category") or "other").strip()
        description = (ph.get("description") or "").strip()
        line = f"  [{i}] category={category} marker_text={marker!r}"
        if description:
            line += f"\n      description: {description}"
        placeholder_lines.append(line)

    if not placeholder_lines:
        return ResolverResult(decisions=[])

    user_prompt = _USER_TEMPLATE.format(
        section_id=section_id or "(unknown)",
        section_title=section_title or "(unknown)",
        n_markers=len(placeholder_lines),
        placeholders_block="\n".join(placeholder_lines),
        profile_summary=profile_summary or "(no profile)",
        decisions_text=decisions_text or "(no decisions)",
        team_roster_block=team_roster_block or "(no approved team)",
        cost_build_block=cost_build_block or "(no cost build)",
    )

    tool_input, usage = call_tool_for_model(
        model=settings.model_needs_human_resolver,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=4000,
        agent_name="needs_human_resolver",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") in ("max_tokens", "length"):
        n_partial = len(tool_input.get("decisions") or [])
        raise RuntimeError(
            f"needs_human_resolver: output truncated at "
            f"max_tokens (in={usage['input_tokens']}, "
            f"out={usage['output_tokens']}). Got {n_partial} "
            f"partial decision(s) before truncation."
        )

    decisions: list[ResolverDecision] = []
    for d in tool_input.get("decisions") or []:
        try:
            marker = str(d.get("marker_text") or "").strip()
            if not marker:
                continue
            action = str(d.get("action") or "skip").lower()
            if action not in _VALID_ACTIONS:
                action = "skip"
            value = str(d.get("value") or "").strip()
            reason = str(d.get("reason") or "").strip()
            decisions.append(
                ResolverDecision(
                    marker_text=marker,
                    action=action,
                    value=value,
                    reason=reason,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "needs_human_resolver: skipping malformed decision %r: %s",
                d,
                exc,
            )

    n_edit = sum(1 for d in decisions if d.action == "edit")
    n_reject = sum(1 for d in decisions if d.action == "reject")
    n_skip = sum(1 for d in decisions if d.action == "skip")
    log.info(
        "needs_human_resolver: section %s — %d input(s), %d edit / %d reject / %d skip (%s)",
        section_id,
        len(placeholders),
        n_edit,
        n_reject,
        n_skip,
        fmt_llm_usage(usage),
    )
    return ResolverResult(decisions=decisions)


__all__ = [
    "ResolverDecision",
    "ResolverResult",
    "resolve_placeholders",
]
