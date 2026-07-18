"""Fact extraction from KB documents → ProfileSuggestion rows.

Runs after a KB document is ingested. Uses Haiku-class extraction (cheap,
structured tool-use output) per document class. Suggestions are NEVER
auto-applied to company_profile.json — they queue for human review on the
Config page.

Class → extractor map:
- corporate                  → suggests cert/NAICS/specialization/differentiator additions
- personnel                  → suggests adding a new person to key_personnel,
                               or merging new attributes into an existing one
- past_performance_won/subbed → suggests adding a project to past_performance,
                                or merging new attributes into an existing one

No extraction for: prior_proposal_*, agency_context, boilerplate, compliance_evidence.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.core.company_profile import reload_company_profile
from app.core.enums import KbDocumentClass
from app.db.session import session_scope
from app.models import ProfileSuggestion
from app.services.llm import get_anthropic

log = logging.getLogger(__name__)


# ---------- Tool specs --------------------------------------------------------

_PERSONNEL_TOOL: dict = {
    "name": "report_person",
    "description": (
        "Extract structured personnel facts from a resume or bio document. "
        "Only report fields explicitly stated. Use null/empty for anything not stated."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "role": {"type": ["string", "null"]},
            "years_experience": {"type": ["integer", "null"]},
            "location": {"type": ["string", "null"]},
            "veteran_status": {"type": ["string", "null"]},
            "certifications": {"type": "array", "items": {"type": "string"}},
            "clearances": {"type": "array", "items": {"type": "string"}},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "past_roles": {"type": "array", "items": {"type": "string"}},
            "education": {"type": "array", "items": {"type": "string"}},
            "tech": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
    },
}

_CORPORATE_TOOL: dict = {
    "name": "report_corporate_facts",
    "description": (
        "Extract structured company facts from a capability statement, company "
        "bio, or website content. Only report items explicitly stated. Empty arrays "
        "for sections not present."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "certifications": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Business certifications (e.g., 'Federal Small Business', 'PA SDB', 'SDVOSB').",
            },
            "naics_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "NAICS codes as 6-digit strings.",
            },
            "deep_specializations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Deep specialization domains (e.g., 'Medicare payment systems').",
            },
            "differentiators": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Stated differentiators or value propositions.",
            },
        },
        "required": [],
    },
}

_PAST_PERF_TOOL: dict = {
    "name": "report_past_performance",
    "description": (
        "Extract one or more past-performance project records from the document. "
        "Each record describes a single contract or engagement."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "projects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project or contract name."},
                        "customer": {"type": ["string", "null"]},
                        "role": {
                            "type": ["string", "null"],
                            "description": "Quadratic's role (prime, subcontractor to X, SME, etc.).",
                        },
                        "scope": {"type": ["string", "null"], "description": "1-3 sentence scope summary."},
                        "status": {
                            "type": ["string", "null"],
                            "description": "active, completed, ongoing, etc., if stated.",
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["project"],
                },
            }
        },
        "required": ["projects"],
    },
}


# ---------- Helpers -----------------------------------------------------------


def _call_tool(*, tool: dict, system: str, prompt: str, agent_name: str) -> dict[str, Any]:
    settings = get_settings()
    client = get_anthropic()
    tool_input, _usage = client.call_tool(
        model=settings.model_light_extraction,  # Haiku
        system=system,
        messages=[{"role": "user", "content": prompt}],
        tool=tool,
        max_tokens=4000,
        agent_name=agent_name,
        proposal_id=None,
    )
    return tool_input


def _upsert_suggestion(
    *,
    document_id: int,
    operation: str,
    section: str,
    match_key: str | None,
    proposed_value: Any,
    current_value: Any,
    summary: str,
    rationale: str | None = None,
) -> bool:
    """Create a ProfileSuggestion row or update the existing pending one for
    the same (document, section, match_key). Returns True if a new row was
    created, False if an existing pending one was updated."""
    with session_scope() as db:
        existing = (
            db.query(ProfileSuggestion)
            .filter(
                ProfileSuggestion.kb_document_id == document_id,
                ProfileSuggestion.section == section,
                ProfileSuggestion.match_key == match_key,
                ProfileSuggestion.status == "pending",
            )
            .first()
        )
        if existing is not None:
            existing.operation = operation
            existing.proposed_value_json = proposed_value
            existing.current_value_json = current_value
            existing.summary = summary
            existing.rationale = rationale
            return False
        s = ProfileSuggestion(
            kb_document_id=document_id,
            operation=operation,
            section=section,
            match_key=match_key,
            proposed_value_json=proposed_value,
            current_value_json=current_value,
            summary=summary,
            rationale=rationale,
            status="pending",
        )
        db.add(s)
        return True


def _strip_empty(d: dict) -> dict:
    """Drop keys with empty/None values so suggestions stay clean."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _list_diff(existing: list, new: list) -> list:
    """Return items in `new` that are not already in `existing` (case-insensitive
    string compare; preserves the new-list casing)."""
    if not new:
        return []
    if not existing:
        return list(new)
    existing_lower = {str(e).strip().lower() for e in existing}
    return [x for x in new if str(x).strip().lower() not in existing_lower]


# ---------- Per-class extractors ---------------------------------------------


def _extract_personnel(document_id: int, text: str) -> int:
    person = _call_tool(
        tool=_PERSONNEL_TOOL,
        system=(
            "You extract personnel facts from a resume or bio document. "
            "Quote roles and credentials only when explicitly stated. Do not infer."
        ),
        prompt=f"Extract person info from this document:\n\n{text[:60000]}",
        agent_name="kb_facts_personnel",
    )
    name = (person.get("name") or "").strip()
    if not name:
        log.info("kb_facts personnel: no name extracted for doc %d", document_id)
        return 0

    profile = reload_company_profile()
    existing = None
    for p in profile.get("key_personnel", []):
        if (p.get("name") or "").strip().lower() == name.lower():
            existing = p
            break

    if existing is None:
        proposed = _strip_empty(person)
        role_str = f" ({person.get('role')})" if person.get("role") else ""
        _upsert_suggestion(
            document_id=document_id,
            operation="append",
            section="key_personnel",
            match_key=name,
            proposed_value=proposed,
            current_value=None,
            summary=f"Add new person: {name}{role_str}",
        )
        return 1

    # Existing person — additive merge only.
    merge_fields: dict[str, Any] = {}
    for field in ("certifications", "clearances", "strengths", "past_roles", "education", "tech"):
        new_items = person.get(field) or []
        if not new_items:
            continue
        additions = _list_diff(existing.get(field) or [], new_items)
        if additions:
            merge_fields[field] = (existing.get(field) or []) + additions

    for field in ("role", "years_experience", "location", "veteran_status"):
        new_v = person.get(field)
        if new_v and not existing.get(field):
            merge_fields[field] = new_v

    if not merge_fields:
        log.info("kb_facts personnel: no new info for %s in doc %d", name, document_id)
        return 0

    _upsert_suggestion(
        document_id=document_id,
        operation="merge",
        section="key_personnel",
        match_key=name,
        proposed_value=merge_fields,
        current_value=existing,
        summary=f"Update {name}: {', '.join(merge_fields.keys())}",
    )
    return 1


_CORP_LIST_FIELDS = {
    "certifications": ("certifications", "Add certification: {item}"),
    "deep_specializations": ("deep_specializations", "Add specialization: {item}"),
    "differentiators": (
        "differentiators_for_proposals",
        "Add differentiator: {item}",
    ),
}


def _extract_corporate(document_id: int, text: str) -> int:
    facts = _call_tool(
        tool=_CORPORATE_TOOL,
        system=(
            "You extract company-level facts from a capability statement, "
            "company bio, or website content. Only report items explicitly stated."
        ),
        prompt=f"Extract corporate facts from this document:\n\n{text[:60000]}",
        agent_name="kb_facts_corporate",
    )
    profile = reload_company_profile()
    created = 0

    # Simple list fields
    for src_key, (target_section, summary_tmpl) in _CORP_LIST_FIELDS.items():
        new_items = facts.get(src_key) or []
        if not new_items:
            continue
        existing = profile.get(target_section) or []
        additions = _list_diff(existing, new_items)
        for item in additions:
            if _upsert_suggestion(
                document_id=document_id,
                operation="append",
                section=target_section,
                match_key=item,  # use item itself as the de-dup key
                proposed_value=item,
                current_value=existing,
                summary=summary_tmpl.format(item=item),
            ):
                created += 1

    # NAICS — a nested object {primary, secondary, opportunistic}.
    new_naics = facts.get("naics_codes") or []
    if new_naics:
        naics_section = profile.get("naics", {}) or {}
        existing_all = (
            (naics_section.get("primary") or [])
            + (naics_section.get("secondary") or [])
            + (naics_section.get("opportunistic") or [])
        )
        additions = _list_diff(existing_all, new_naics)
        for code in additions:
            if _upsert_suggestion(
                document_id=document_id,
                operation="append",
                section="naics.opportunistic",
                match_key=code,
                proposed_value=code,
                current_value=naics_section,
                summary=f"Add NAICS code: {code} (opportunistic)",
                rationale="New NAICS codes default to the 'opportunistic' bucket; promote to primary/secondary on review.",
            ):
                created += 1

    return created


def _extract_past_performance(document_id: int, text: str, document_class: KbDocumentClass) -> int:
    facts = _call_tool(
        tool=_PAST_PERF_TOOL,
        system=(
            "You extract past-performance project records from a document. "
            "One record per distinct project. Only report fields explicitly stated."
        ),
        prompt=f"Extract project records from this document:\n\n{text[:60000]}",
        agent_name="kb_facts_past_performance",
    )
    projects = facts.get("projects") or []
    if not projects:
        return 0

    profile = reload_company_profile()
    existing = profile.get("past_performance") or []
    existing_by_name = {(p.get("project") or "").strip().lower(): p for p in existing}

    citation_class = document_class.value
    created = 0
    for proj in projects:
        name = (proj.get("project") or "").strip()
        if not name:
            continue
        proposed = _strip_empty(proj)
        proposed["citation_class"] = citation_class
        existing_proj = existing_by_name.get(name.lower())

        if existing_proj is None:
            if _upsert_suggestion(
                document_id=document_id,
                operation="append",
                section="past_performance",
                match_key=name,
                proposed_value=proposed,
                current_value=None,
                summary=f"Add past performance: {name}",
            ):
                created += 1
        else:
            # Merge: only fill in fields that are empty on the existing record.
            merge_fields: dict[str, Any] = {}
            for k, v in proposed.items():
                if k == "tags":
                    additions = _list_diff(existing_proj.get("tags") or [], v or [])
                    if additions:
                        merge_fields["tags"] = (existing_proj.get("tags") or []) + additions
                    continue
                if v and not existing_proj.get(k):
                    merge_fields[k] = v
            if not merge_fields:
                continue
            if _upsert_suggestion(
                document_id=document_id,
                operation="merge",
                section="past_performance",
                match_key=name,
                proposed_value=merge_fields,
                current_value=existing_proj,
                summary=f"Update past performance — {name}: {', '.join(merge_fields.keys())}",
            ):
                created += 1

    return created


# ---------- Entry point -------------------------------------------------------


def extract_profile_suggestions(*, document_id: int, document_class: KbDocumentClass, text: str) -> int:
    """Dispatcher. Returns count of new suggestion rows created."""
    if not text or not text.strip():
        return 0

    # SQLAlchemy returns String-mapped enum columns as plain `str` despite
    # the `Mapped[KbDocumentClass]` type hint, so callers reading the
    # column off a model instance hand us a string. Coerce here once so
    # downstream code can safely use `.value`, equality, and membership
    # without each function defending against both shapes.
    if not isinstance(document_class, KbDocumentClass):
        try:
            document_class = KbDocumentClass(document_class)
        except ValueError:
            log.warning(
                "extract_profile_suggestions: unknown document_class=%r — skipping fact extraction.",
                document_class,
            )
            return 0

    if document_class == KbDocumentClass.PERSONNEL:
        return _extract_personnel(document_id, text)
    if document_class == KbDocumentClass.CORPORATE:
        return _extract_corporate(document_id, text)
    if document_class in (
        KbDocumentClass.PAST_PERFORMANCE_WON,
        KbDocumentClass.PAST_PERFORMANCE_SUBBED,
    ):
        return _extract_past_performance(document_id, text, document_class)

    # No fact extraction for prior_proposal_*, agency_context, boilerplate,
    # compliance_evidence — the design doc explicitly excludes these from
    # citation/profile updates.
    return 0
