"""Grounded credential verification — Reviewer A's web-grounded check.

Catches the failure mode that's most expensive at submission time: a
fabricated certification on a Quadratic proposal. Two layers:

1. PROFILE CROSS-CHECK (deterministic, every pass). Extract certification
   mentions from the section draft via regex against a known list. Any
   cert that appears in the draft but does NOT have substring overlap
   with any entry in `company_profile.certifications` is flagged
   CRITICAL — the writer hallucinated it. Runs on every review pass
   because it's free.

2. WEB GROUNDING (Gemini Pro + Google Search, pass 1 only). For certs
   that survived the profile cross-check, build a single grounded
   Gemini query asking whether public sources substantiate Quadratic
   holding each credential. Catches the case where the profile is
   itself wrong (cert lapsed, never was, etc.). Findings are MAJOR
   (not CRITICAL) — false negatives are common for small businesses
   with thin web presence, so we err toward review-not-block. Only
   pass 1 because credentials don't change between revisions of the
   same draft, and a $0.05 Gemini call per section per pass adds up.

Findings persist as reviewer_agent='A' alongside the existing pre-
flight checks. Auto-loop's normal flow then handles them (auto-accept
CRITICAL, surface in directive to writer, regenerate).
"""

from __future__ import annotations

import logging
import re

from app.agents.reviewer_a import ReviewerFindingDraft
from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.db.session import session_scope
from app.models import ProposalSection
from app.services.findings import get_pass_number_for_section
from app.services.llm import GeminiSync

log = logging.getLogger(__name__)


# Cert patterns. Each tuple is (regex, canonical_name). The regex
# captures the full mention as it appears in the draft (including any
# qualifying level like "High", "Type 2"); the canonical name is what
# we use to query Gemini and to display in findings. Case-insensitive
# match — RFPs and drafts capitalize inconsistently.
#
# Keep this list focused on credentials that are commonly cited in
# public-sector IT proposals. Adding obscure ones risks false-positive
# extraction on words that incidentally match.
_CERT_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bFedRAMP(?:\s+(?:Low|Moderate|High))?\b", re.IGNORECASE), "FedRAMP"),
    (re.compile(r"\bSOC\s*2(?:\s+Type\s*[12])?\b", re.IGNORECASE), "SOC 2"),
    (re.compile(r"\bISO\s*27001\b", re.IGNORECASE), "ISO 27001"),
    (re.compile(r"\bISO\s*27017\b", re.IGNORECASE), "ISO 27017"),
    (re.compile(r"\bISO\s*27018\b", re.IGNORECASE), "ISO 27018"),
    (re.compile(r"\bHITRUST(?:\s+CSF)?\b", re.IGNORECASE), "HITRUST"),
    (re.compile(r"\bNIST\s*800-53\b", re.IGNORECASE), "NIST 800-53"),
    (re.compile(r"\bNIST\s*800-171\b", re.IGNORECASE), "NIST 800-171"),
    (re.compile(r"\bCMMC(?:\s+Level\s*[123])?\b", re.IGNORECASE), "CMMC"),
    (re.compile(r"\bFISMA(?:\s+(?:Low|Moderate|High))?\b", re.IGNORECASE), "FISMA"),
    (re.compile(r"\bStateRAMP\b", re.IGNORECASE), "StateRAMP"),
    (re.compile(r"\bDoD\s*SRG\b", re.IGNORECASE), "DoD SRG"),
    # Small business set-asides — these are claimed certifications too,
    # and "we are 8(a)-certified" when we aren't is just as actionable
    # as a fabricated security cert.
    # No trailing \b on 8(a): ')' is a non-word char and \b requires a
    # word↔non-word transition; in "8(a) certified" the next char is a
    # space (also non-word), so trailing \b never fires.
    (re.compile(r"\b8\(a\)", re.IGNORECASE), "8(a)"),
    (re.compile(r"\bHUBZone\b", re.IGNORECASE), "HUBZone"),
    (re.compile(r"\bWOSB\b", re.IGNORECASE), "WOSB"),
    (re.compile(r"\bSDVOSB\b", re.IGNORECASE), "SDVOSB"),
)


def _extract_cert_mentions(text: str) -> dict[str, str]:
    """Return {canonical_name: first_full_mention} for every cert
    pattern that fires on the text. The full mention preserves the
    qualifier ('FedRAMP High') so the finding can quote what the writer
    actually wrote."""
    out: dict[str, str] = {}
    for pattern, canonical in _CERT_PATTERNS:
        m = pattern.search(text or "")
        if m and canonical not in out:
            out[canonical] = m.group(0)
    return out


def _cert_in_profile(cert_canonical: str, profile_certs: list[str]) -> bool:
    """Profile entries are free-form strings like 'FedRAMP' or 'DoD SRG
    Compliance' or 'PA Small Diverse Business (SDB)'. Match a draft-
    extracted canonical cert against any profile entry via case-
    insensitive substring containment in either direction (handles both
    'FedRAMP' in 'FedRAMP High' and 'DoD SRG' in 'DoD SRG Compliance')."""
    needle = cert_canonical.lower()
    for entry in profile_certs:
        hay = (entry or "").lower()
        if not hay:
            continue
        if needle in hay or hay in needle:
            return True
    return False


def _profile_cert_strings() -> list[str]:
    """Pull cert strings out of company_profile.certifications. Tolerant
    of either string entries or {name: ...} dict entries (keeps room for
    the schema to evolve without breaking the cross-check)."""
    prof = get_company_profile()
    out: list[str] = []
    for c in prof.get("certifications") or []:
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, dict):
            name = c.get("name") or c.get("title")
            if name:
                out.append(str(name))
    return out


_GROUNDING_SYSTEM = """You are a credential-verification assistant. Given a list of certifications a small public-sector IT firm CLAIMS to hold, search the web (Google Search grounding is enabled) for authoritative public sources — FedRAMP Marketplace, SAM.gov, ISO directories, CMMC accreditation body listings, state procurement registries, the firm's own official website, etc. For each cert, judge:

- "VERIFIED" — found at least one authoritative public source confirming the firm holds it.
- "UNVERIFIED" — searched but found no evidence either way. Common for small businesses with thin web presence; do NOT treat as proof of fabrication.
- "CONTRADICTED" — found evidence that the firm does NOT hold this credential (lapsed, never granted, different vendor with similar name, etc.).

Be conservative on CONTRADICTED — only assert it when you have a clear public source disputing the claim. UNVERIFIED is the safe default when sources are silent.

Return a plain-text response with one line per cert, format: `<canonical_name>: <verdict> — <one-line evidence summary or 'no public source found'>`. No preamble, no markdown."""


_GROUNDING_USER_TEMPLATE = """Firm: {legal_name}
UEI: {uei}
CAGE Code: {cage_code}
Website: {website}

Verify whether this firm holds each of the following certifications. Use Google Search to consult authoritative public sources.

Certifications to verify:
{cert_list}

Return one line per certification in the format specified."""


def _verdict_for(line: str, cert_canonical: str) -> str | None:
    """Pull the verdict word out of a Gemini response line. None when
    the line can't be parsed."""
    line_l = line.lower()
    if cert_canonical.lower() not in line_l:
        return None
    for verdict in ("verified", "unverified", "contradicted"):
        if verdict in line_l:
            return verdict.upper()
    return None


def check_section_credentials_grounded(
    section_pk: int,
) -> list[ReviewerFindingDraft]:
    """Run the grounded credential verification pre-flight on one
    drafted section. Returns ReviewerFindingDraft entries that the
    caller persists with reviewer_agent='A'.

    Layer 1 (every pass): cert in draft but not in profile → CRITICAL.
    Layer 2 (pass 1 only): web-verify all draft certs via Gemini
    grounded; flag CONTRADICTED as MAJOR. Layer 2 is best-effort —
    Gemini failures log + return early without crashing the section.
    """
    findings: list[ReviewerFindingDraft] = []

    with session_scope() as db:
        sec = db.get(ProposalSection, section_pk)
        if sec is None:
            return findings
        draft_text = sec.draft_text_markdown or ""
        section_id = sec.section_id

    if not draft_text.strip():
        return findings

    mentions = _extract_cert_mentions(draft_text)
    if not mentions:
        return findings

    profile_certs = _profile_cert_strings()

    # ---- Layer 1: profile cross-check (deterministic, every pass) ----
    fabricated: list[tuple[str, str]] = []  # (canonical, full_mention)
    in_profile: list[tuple[str, str]] = []
    for canonical, full_mention in mentions.items():
        if _cert_in_profile(canonical, profile_certs):
            in_profile.append((canonical, full_mention))
        else:
            fabricated.append((canonical, full_mention))

    for canonical, full_mention in fabricated:
        findings.append(
            ReviewerFindingDraft(
                severity="CRITICAL",
                category="hallucination",
                finding_text=(
                    f"Section {section_id} claims '{full_mention}' — but "
                    f"this credential is NOT present in "
                    f"company_profile.certifications. Quadratic does not "
                    f"hold this credential according to the canonical "
                    f"profile. Asserting it in a federal proposal is a "
                    f"FAR-actionable misrepresentation."
                ),
                suggested_fix=(
                    f"Either (a) remove the '{canonical}' claim from this "
                    f"section entirely, (b) rewrite to remove the "
                    f"credential assertion (e.g., 'we follow NIST 800-53 "
                    f"controls' instead of 'we are NIST 800-53 certified' "
                    f"if the framing is truthful as a practice rather than "
                    f"a credential), or (c) if Quadratic actually does "
                    f"hold this credential, update "
                    f"company_profile.certifications first via the Profile "
                    f"page before re-claiming it in any draft."
                ),
            )
        )

    # ---- Layer 2: web grounding (pass 1 only) -------------------------
    # Skip on subsequent passes — credentials don't change between
    # revisions of the same draft, and a $0.05 Gemini call × N sections
    # × M passes adds up. Pass 0 means no findings yet (first run).
    next_pass = get_pass_number_for_section(section_pk) + 1
    if next_pass > 1:
        return findings
    if not in_profile:
        # Nothing to grounding-verify (the fabricated set is already
        # CRITICAL via Layer 1; Gemini won't add signal beyond that).
        return findings

    settings = get_settings()
    profile = get_company_profile()
    company = profile.get("company", {})
    cert_list = "\n".join(f"- {canonical} (as written in draft: '{full}')" for canonical, full in in_profile)

    user_prompt = _GROUNDING_USER_TEMPLATE.format(
        legal_name=company.get("legal_name", "Quadratic Digital LLC"),
        uei=company.get("uei", "(unknown)"),
        cage_code=company.get("cage_code", "(unknown)"),
        website=company.get("website", "(unknown)"),
        cert_list=cert_list,
    )

    try:
        gemini = GeminiSync()
        text, _citations, _usage = gemini.complete_with_search(
            model=settings.model_teaming_researcher,
            system=_GROUNDING_SYSTEM,
            user_prompt=user_prompt,
            max_tokens=2000,
            agent_name="grounding_check_credentials",
            proposal_id=None,  # not tied to a single proposal at this layer
        )
    except Exception:
        log.exception(
            "grounding_check: Gemini grounded call failed for section %s "
            "— proceeding with Layer 1 findings only",
            section_id,
        )
        return findings

    if not (text or "").strip():
        return findings

    # Parse one line per cert — Gemini's response should be plain text
    # in the format "Cert: VERDICT — explanation". We're tolerant of
    # missing certs (Gemini may merge or drop) and only act on lines
    # that clearly say CONTRADICTED.
    for line in (raw_line.strip() for raw_line in text.splitlines() if raw_line.strip()):
        for canonical, full_mention in in_profile:
            verdict = _verdict_for(line, canonical)
            if verdict != "CONTRADICTED":
                continue
            findings.append(
                ReviewerFindingDraft(
                    severity="MAJOR",
                    category="hallucination",
                    finding_text=(
                        f"Web grounding contradicts the '{full_mention}' "
                        f"claim in section {section_id}. The "
                        f"company_profile says Quadratic holds this "
                        f"credential, but a Google-Search-grounded "
                        f"verification found public sources disputing it. "
                        f"This may indicate the profile is out of date "
                        f"(cert lapsed, never granted, etc.) or that the "
                        f"web-grounding pass returned a false negative — "
                        f"verify before submission. Gemini's response on "
                        f'this cert: "{line[:240]}"'
                    ),
                    suggested_fix=(
                        "Verify directly with the issuing authority "
                        f"whether Quadratic still holds {canonical}. If "
                        f"yes: dismiss this finding and consider adding "
                        f"clarifying language in the draft (e.g., issuing "
                        f"date, scope) so future automated checks have "
                        f"more to work with. If no: remove the claim from "
                        f"all sections AND update "
                        f"company_profile.certifications via the Profile "
                        f"page so future drafts don't re-introduce it."
                    ),
                )
            )
            break  # one finding per cert, even if multiple lines match

    return findings


__all__ = ["check_section_credentials_grounded"]
