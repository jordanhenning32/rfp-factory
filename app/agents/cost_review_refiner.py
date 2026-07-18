"""Cost Review Refiner — interactive AI helper for Cost Review
findings.

The user invokes this from the Cost Review tab's "Refine with AI"
button. They provide a piece of context the original Cost Reviewer
didn't have (e.g., "the SSP is delivered by a sub, not in-house"),
and the refiner rewrites the recommended_change to incorporate that
context. The refined recommendation is shown in the dialog; the
user can iterate or save it as user_note (which becomes the
displayed recommendation when user_action=accepted).

Single Sonnet 4.6 call returning free-form text (just the new
recommendation — no JSON, no preamble). Cheap and fast for an
interactive use case.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.services.llm import get_anthropic

log = logging.getLogger(__name__)


_SYSTEM = """You refine cost-review recommendations based on user-provided context.

You will be given:
  - A finding from the Cost Reviewer (severity, category, subject, full description).
  - The CURRENT recommended_change (what the agent or user has so far).
  - The user's GUIDANCE — context the original reviewer didn't have, OR a direction the user wants the recommendation to take.

Your job: produce ONE refined recommended_change that incorporates the user's guidance. Output ONLY the new recommendation text — no preamble, no headers, no explanation. Same format and discipline as the original recommendation:

  - Concrete and actionable. "Increase X from N to M" / "Drop Y salary from A to B" / "Add ODC for [item] at $Z" / "Reduce margin from P% to Q%". Not abstract.
  - Quantified when possible. Specific hours, dollars, percentages.
  - One change per recommendation. If multiple changes are needed, pick the simplest one that addresses the finding given the user's guidance.
  - Honor the user's guidance literally. If they say "the SSP is delivered by a sub", do NOT recommend increasing in-house Security Consultant hours; recommend a subcontractor cost line or a coordination role instead.
  - Don't restate the finding. The recommendation is the FIX, not a recap of the problem.
  - Don't editorialize. No "Great point!" or "Considering your input...". Just the new recommendation.

If the user's guidance contradicts the original finding entirely (e.g., "this isn't actually a problem"), you may produce a recommendation that REJECTS or DOWNGRADES the finding — but stay grounded in what the user said. Don't invent reasons."""


_USER_TEMPLATE = """=== Finding ===
Severity: {severity}
Category: {category}
Subject: {subject}

Description:
{finding_text}

=== Current recommended_change ===
{current_recommendation}

=== User's guidance ===
{user_guidance}

Output the refined recommended_change now. Only the new recommendation text — no preamble."""


def refine_recommendation(
    *,
    proposal_id: int,
    severity: str,
    category: str,
    subject: str,
    finding_text: str,
    current_recommendation: str,
    user_guidance: str,
) -> str:
    """Refine a Cost Review finding's recommended_change based on
    user-provided guidance. Returns the new recommendation text
    (free-form, no JSON wrapping).

    Logs cost to agent_runs via the standard llm.py path. Caller
    handles errors — typically catches and shows an error notification
    to the user."""
    settings = get_settings()
    user_prompt = _USER_TEMPLATE.format(
        severity=severity,
        category=category,
        subject=subject or "(no subject)",
        finding_text=(finding_text or "").strip(),
        current_recommendation=(current_recommendation or "(no current recommendation)").strip(),
        user_guidance=user_guidance.strip(),
    )

    text, usage = get_anthropic().complete(
        model=settings.model_cost_review_refiner,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=1500,
        agent_name="cost_review_refiner",
        proposal_id=proposal_id,
    )

    refined = (text or "").strip()
    log.info(
        "cost_review_refiner: proposal %d, %d chars guidance, %d chars refined output, cost $%.4f",
        proposal_id,
        len(user_guidance),
        len(refined),
        usage.get("cost_usd", 0.0),
    )
    return refined


__all__ = ["refine_recommendation"]
