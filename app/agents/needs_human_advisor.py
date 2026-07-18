"""Needs-Human Advisor — Haiku-powered single-shot suggester.

When the user clicks 'Provide value' on a [NEEDS_HUMAN] placeholder in the
Draft tab, this agent proposes a concrete replacement string the user can
either paste in directly or edit before applying.

Designed to be cheap (~$0.005 per call) and fast (~1-3s) so it can run on
every dialog open without ceremony. Uses Haiku — Sonnet is overkill for a
single-sentence suggestion.

Honesty rules still apply: never invent certifications, dates, dollar
amounts, or partner confirmations. When the placeholder asks for a number
the agent can't defensibly produce, it suggests deferring language rather
than fabricating one.
"""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.db.session import SessionLocal
from app.models import ProposalSection
from app.services.llm import get_anthropic

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "report_suggested_replacement",
    "description": (
        "Suggest one concrete replacement string for a [NEEDS_HUMAN] placeholder in a proposal draft."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "suggestion": {
                "type": "string",
                "description": (
                    "Replacement text to insert at the marker location. "
                    "Plain text — no [NEEDS_HUMAN: …] wrapping, no [^cite-N] "
                    "markers. 1-3 sentences typically. Match the surrounding "
                    "section's tone and tense. If the placeholder asks for a "
                    "number, dollar amount, date, or commitment that you "
                    "can't defensibly produce from the company profile or "
                    "section context, propose deferring language (e.g. "
                    "'pending Cost Analysis', 'before submission, contingent "
                    "on executive sign-off') rather than inventing a value."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "1 short sentence on why this suggestion fits — "
                    "what evidence you grounded it in, or what assumption "
                    "you made if you couldn't ground it firmly."
                ),
            },
        },
        "required": ["suggestion"],
    },
}


_SYSTEM = """You are an expert proposal writer for Quadratic Digital, a small public-sector software firm. Your job: given ONE [NEEDS_HUMAN] placeholder and the section context around it, suggest a concrete replacement string the human reviewer can drop into the draft.

You are an assistant for the human reviewer, not the original section drafter. The human will read your suggestion, possibly edit, then apply it. The human may also follow up with refinement requests ("make it shorter", "use 6 months instead", "why did you suggest that?", "now make it more formal") — read the conversation history and respond directly to the latest request, then provide an updated suggestion.

RULES:
1. Honesty constraints (non-negotiable):
   - Never invent certifications, clearances, or past performance. If the company profile doesn't contain it, don't suggest it.
   - Never invent specific dollar amounts, FTE counts, dates, or contractual commitments that aren't already documented somewhere in the inputs.
   - When the placeholder asks for something you can't ground (specific cost, specific schedule date, executive approval), suggest deferring language: '[Insert dollar amount once Cost Analysis Agent produces the budget]', 'pending executive sign-off', 'in alignment with the final negotiated SOW'.

2. Voice:
   - Match Quadratic's small-business voice: confident, direct, plain English.
   - Mirror the surrounding section's tone (formal cover letter vs. technical detail vs. management approach).
   - Use 'we' for Quadratic, third-person for partners.

3. Format:
   - Plain text. No markdown wrappers.
   - No [NEEDS_HUMAN: …] re-wrapping.
   - No [^cite-N] markers — you're filling in user content, not adding citations.
   - 1-3 sentences usually. If the placeholder is a single value (a name, date, dollar amount), output just that value.

4. Grounding:
   - The company profile excerpt is provided. Use it to ground claims.
   - The section excerpt is provided. Match its tone and stay consistent with what it already says.
   - The placeholder description tells you the user's intent. Honor it.

5. Follow-up requests:
   - When the user asks for a refinement (e.g., "make it shorter"), apply the change and produce a new suggestion that incorporates the refinement.
   - When the user asks a question (e.g., "why did you suggest 9-12 months?"), answer it briefly in the rationale field while still providing a suggestion in the suggestion field — even if it's the same as before. The user wants both context and an updated value to apply.
   - When the user proposes alternative content (e.g., "use 6 months"), incorporate it directly into the suggestion if it's defensible, OR push back in the rationale if it conflicts with honesty rules.

OUTPUT: call report_suggested_replacement with one suggestion (the text to paste into the draft) + a short rationale (why this fits, or in follow-ups, the answer to the user's question)."""


_USER_TEMPLATE = """=== PLACEHOLDER ===
marker_text: {marker_text}
description: {description}
category: {category}

=== SECTION CONTEXT (where this placeholder appears) ===
{section_excerpt}

=== COMPANY PROFILE (for grounding factual claims) ===
{profile_excerpt}

Suggest one replacement the user can paste in to resolve this placeholder."""


# Truncation caps — Haiku context is large but we want this call fast + cheap.
_MAX_SECTION_CHARS = 6_000
_MAX_PROFILE_CHARS = 8_000


def _fetch_seed_context(section_pk: int) -> tuple[str, str]:
    """Pull the section markdown excerpt + profile excerpt used to seed the
    initial user message. Same for both single-shot and chat callers."""
    with SessionLocal() as db:
        sec = db.get(ProposalSection, section_pk)
        section_excerpt = (sec.draft_text_markdown if sec else "") or ""
    section_excerpt = section_excerpt[:_MAX_SECTION_CHARS]
    if not section_excerpt:
        section_excerpt = "(no section text available)"
    profile_excerpt = json.dumps(get_company_profile(), indent=2)[:_MAX_PROFILE_CHARS]
    return section_excerpt, profile_excerpt


def chat_about_placeholder(
    *,
    proposal_id: int,
    section_pk: int,
    marker_text: str,
    description: str,
    category: str,
    history: list[dict] | None = None,
    user_message: str | None = None,
) -> dict:
    """One LLM call for a NEEDS_HUMAN placeholder. Supports both initial
    single-shot suggestions and conversational refinement.

    `history` is the alternating prior turns AFTER the initial seed
    (the seed user message is built internally — never include it in
    history). Each turn is {role: 'user'|'assistant', content: str}.
    Empty / None for the initial call.

    `user_message` is the new user input for follow-up calls. None for
    the initial call.

    Returns {suggestion: str, rationale: str}.
    """
    settings = get_settings()
    client = get_anthropic()

    section_excerpt, profile_excerpt = _fetch_seed_context(section_pk)

    seed = _USER_TEMPLATE.format(
        marker_text=marker_text,
        description=description or "(none)",
        category=category or "other",
        section_excerpt=section_excerpt,
        profile_excerpt=profile_excerpt,
    )

    messages: list[dict] = [{"role": "user", "content": seed}]

    if history:
        for turn in history:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    if user_message and user_message.strip():
        # Maintain user/assistant alternation. If the last message is
        # already from the user (e.g., history starts with the seed +
        # nothing else), concatenate rather than appending a second user
        # turn (Anthropic's API rejects user→user).
        if messages[-1]["role"] == "user":
            messages[-1]["content"] = messages[-1]["content"] + "\n\n" + user_message.strip()
        else:
            messages.append({"role": "user", "content": user_message.strip()})

    tool_input, usage = client.call_tool(
        model=settings.model_light_extraction,  # Haiku — fast + cheap
        system=_SYSTEM,
        messages=messages,
        tool=_TOOL,
        max_tokens=600,
        agent_name="needs_human_advisor",
        proposal_id=proposal_id,
    )

    suggestion = str(tool_input.get("suggestion") or "").strip()
    rationale = str(tool_input.get("rationale") or "").strip()
    log.info(
        "needs_human_advisor: marker=%r turns=%d -> %d-char suggestion (cost=$%.4f)",
        marker_text[:60],
        len(messages),
        len(suggestion),
        usage["cost_usd"],
    )
    return {"suggestion": suggestion, "rationale": rationale}


# Backwards-compatible alias for the old single-shot API. New callers
# should use chat_about_placeholder() so they can pass history/user_message
# when conversation extends past the initial suggestion.
def suggest_replacement(
    *,
    proposal_id: int,
    section_pk: int,
    marker_text: str,
    description: str,
    category: str,
) -> dict:
    return chat_about_placeholder(
        proposal_id=proposal_id,
        section_pk=section_pk,
        marker_text=marker_text,
        description=description,
        category=category,
    )
