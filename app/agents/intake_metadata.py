"""Intake-time metadata extraction.

Pre-populates the New Proposal form from RFP text so the user doesn't type
in things the document already states. Cheap Haiku-class call, ~10 pages of
context, structured JSON out.

This is NOT the Compliance Matrix Agent — it only pulls high-level metadata
(title, agency, NAICS, due date, solicitation number). The full compliance
extraction happens after the user clicks Run.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass

from app.config import get_settings
from app.services.llm import fmt_llm_usage, get_anthropic

log = logging.getLogger(__name__)

_SYSTEM = (
    "You extract structured metadata from government RFP / solicitation documents. "
    "You output ONLY a JSON object — no commentary, no markdown fences. "
    "Every field that you cannot find with reasonable confidence in the text MUST be null. "
    "The service_line field is the exception: pick the best-fitting category from the "
    "registered options listed in the prompt; never invent a new value, but always pick one."
)


def _build_prompt(text: str) -> str:
    """Compose the user prompt with the service-line registry options
    inlined so Haiku can classify against the current set without a
    code change when the registry grows. Lazy-imported to avoid a
    cycle (service_line → models → ...)."""
    from app.services.service_line import list_service_lines

    options_block = "\n".join(f'    "{sl["id"]}" — {sl["description"]}' for sl in list_service_lines())
    valid_ids = ", ".join(f'"{sl["id"]}"' for sl in list_service_lines())

    return f"""Extract the following metadata from this RFP / solicitation text. Return ONLY a JSON object with these exact keys:

{{
  "title": "Concise proposal title or solicitation subject (e.g., 'Website Design and Configuration Services'). Not the agency name; the work being procured.",
  "agency": "Issuing agency or department name (e.g., 'Pennsylvania Department of Aging', 'Centers for Medicare & Medicaid Services')",
  "solicitation_number": "RFP / IFB / RFQ / contract number as printed (e.g., 'RFP 758 2600000258'). Null if not present.",
  "naics": "Primary NAICS code as a 6-digit string (e.g., '541511'). Null if not stated.",
  "due_date": "Proposal submission deadline in YYYY-MM-DD format. Convert from any source format. Null if relative ('30 days from issue') or absent.",
  "service_line": "Best-fitting service-line category from the options below. ALWAYS pick one — never null. When in doubt, pick 'it_services'."
}}

Service-line options (pick exactly one of these IDs):
{options_block}

Decision heuristics for service_line:
- Pick "payment_systems" when the RFP centers on credit-card / debit-card processing, POS terminals, online payment portals, ACH/EFT processing, recurring/subscription billing, donation processing, or merchant services. Strong signals: "interchange", "merchant services", "PCI compliance", "payment processor", "POS terminals", "card processing", "ACH/EFT", "lockbox".
- Pick "it_services" for everything else: custom software, cloud migration, system modernization, IV&V, project management, web/app development, data analytics, cybersecurity work, MMIS / EHR / federal IT, etc. This is the default when the RFP is software/services-oriented and does NOT center on payment processing.

Use null for the other fields you cannot determine. Do NOT guess. Do NOT include keys other than the six above. service_line MUST be one of: {valid_ids}.

RFP TEXT:
{text}"""


@dataclass
class ExtractedMetadata:
    title: str | None = None
    agency: str | None = None
    solicitation_number: str | None = None
    naics: str | None = None
    due_date: str | None = None  # YYYY-MM-DD string or None
    service_line: str | None = None  # one of the registered service_line IDs, or None

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def has_anything(self) -> bool:
        return any(v for v in self.as_dict().values())


_FIELD_KEYS = {
    "title",
    "agency",
    "solicitation_number",
    "naics",
    "due_date",
    "service_line",
}


def _coerce_str_or_none(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    # Numbers, etc. — stringify defensively.
    return str(v)


def _parse_response(raw: str) -> ExtractedMetadata:
    # Tolerate Claude wrapping in ```json ... ``` despite system prompt.
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fence_match.group(1) if fence_match else raw

    # Take the first {...} block — defensive against pre/post chatter.
    brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not brace_match:
        log.warning("metadata extractor: no JSON object found in response: %r", raw[:200])
        return ExtractedMetadata()

    try:
        data = json.loads(brace_match.group(0))
    except json.JSONDecodeError:
        log.warning("metadata extractor: JSON parse failed: %r", brace_match.group(0)[:200])
        return ExtractedMetadata()

    if not isinstance(data, dict):
        return ExtractedMetadata()

    # Validate service_line against the registry — drop unknown values
    # so a hallucinated category doesn't poison the dropdown.
    raw_sl = _coerce_str_or_none(data.get("service_line"))
    if raw_sl is not None:
        from app.services.service_line import is_valid_service_line

        if not is_valid_service_line(raw_sl):
            log.warning(
                "metadata extractor: dropping unknown service_line=%r (not registered in SERVICE_LINES)",
                raw_sl,
            )
            raw_sl = None

    return ExtractedMetadata(
        title=_coerce_str_or_none(data.get("title")),
        agency=_coerce_str_or_none(data.get("agency")),
        solicitation_number=_coerce_str_or_none(data.get("solicitation_number")),
        naics=_coerce_str_or_none(data.get("naics")),
        due_date=_coerce_str_or_none(data.get("due_date")),
        service_line=raw_sl,
    )


def extract_metadata_from_text(text: str, *, max_chars: int = 40_000) -> ExtractedMetadata:
    """Synchronous Haiku call to pull metadata. Caller should run in a thread
    if invoked from an async context.
    """
    if not text or not text.strip():
        return ExtractedMetadata()

    settings = get_settings()
    client = get_anthropic()
    prompt = _build_prompt(text[:max_chars])

    response, usage = client.complete(
        model=settings.model_light_extraction,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        agent_name="intake_metadata",
        proposal_id=None,  # Proposal doesn't exist yet at intake time
        temperature=0.0,
    )
    log.info("intake_metadata: %s", fmt_llm_usage(usage))
    return _parse_response(response)
