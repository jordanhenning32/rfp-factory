"""Deterministic pre-flight checks that run BEFORE Reviewer A on every
section. Catches mechanical bugs that the LLM occasionally misses
because its attention is drawn to subtler issues.

Each function returns `list[ReviewerFindingDraft]` matching the shape
that `app.services.findings.persist_findings` expects when called with
`reviewer_agent='A'`.

Sibling module: `app.services.citation_check.check_section_citations`
covers the past-performance-citation legitimacy check (FAR-actionable
bug class). This module covers compliance-coverage gaps (assigned
requirement → no salient terms in the draft).
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from app.agents.reviewer_a import ReviewerFindingDraft
from app.core.company_profile import get_company_profile
from app.db.session import session_scope
from app.models import ComplianceMatrixItem, ProposalSection

log = logging.getLogger(__name__)


# Hard-coded stopword set per spec. Stdlib only — no NLTK / sklearn.
# Skews toward common English connectives + RFP modal verbs ("shall",
# "must", "should", "will", "may"). Words like "section", "system",
# "data", "user" are intentionally NOT stopwords — they carry meaning
# specific to a requirement.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "or",
        "of",
        "to",
        "a",
        "an",
        "in",
        "on",
        "for",
        "with",
        "by",
        "is",
        "are",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "shall",
        "must",
        "should",
        "will",
        "can",
        "may",
        "have",
        "has",
        "had",
        "such",
        "all",
        "any",
        "from",
        "into",
        "as",
        "at",
        "if",
        "it",
        "its",
        "our",
        "their",
        "his",
        "her",
    }
)

# Word boundary tokenizer. \w+ over a lowercased string gives us
# alphanumeric runs, splitting on hyphens and punctuation. Hyphenated
# compounds ("multi-factor") become two tokens, which is fine for
# substring matching against the draft.
_TOKEN_RE = re.compile(r"\b\w+\b")

# Tokens shorter than this are dropped — too short to be discriminative.
# Filters out 2-3 letter acronyms (MFA, API, AI, ML, RFP, FAR, SLA) too,
# which is a known limitation; the algorithm relies on content-word
# coverage, not acronym presence.
_MIN_TOKEN_LEN = 4

# Skip the check when fewer than this many salient terms remain after
# stopword + min-length filtering — the requirement is too short for
# the missing-ratio to mean anything (e.g., "Comply with FAR 52.219-9"
# only has "comply" as a salient term once "FAR" and number tokens are
# filtered).
_MIN_SALIENT_TERMS = 4

# Above this ratio of missing salient terms, flag the section as not
# addressing the requirement. 50% is the spec's starting point. Bump
# upward (e.g., 0.6 / 0.7) if false positives accumulate; the LLM will
# still catch coverage gaps the deterministic check misses.
_MISSING_RATIO_THRESHOLD = 0.5

# How many missing terms to surface in the finding text. More than 5
# becomes noise; the suggested_fix references the full requirement_text
# anyway.
_FINDING_TERM_PREVIEW = 5

_CREDENTIAL_KEYWORDS = (
    "soc 2",
    "soc2",
    "nist 800-53",
    "nist 800 53",
    "fisma",
    "pci-dss level",
    "pci dss level",
    "iso 27001",
    "fedramp high",
    "fedramp moderate",
    "fedramp low",
    "hipaa compliance",
    "sox compliance",
    "itar",
    "cmmc",
)


def _tokenize(text: str) -> list[str]:
    """Lowercase + extract word tokens. Splits on whitespace, hyphens,
    and punctuation."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _salient_terms(text: str) -> list[str]:
    """Return the deduped salient terms from `text` in first-seen order.

    "Salient" = not a stopword AND length >= _MIN_TOKEN_LEN.
    Order is preserved so the finding's missing-terms preview reads
    naturally rather than alphabetically.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in _tokenize(text):
        if tok in _STOPWORDS:
            continue
        if len(tok) < _MIN_TOKEN_LEN:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def check_compliance_coverage(section_pk: int) -> list[ReviewerFindingDraft]:
    """Pre-flight: for each compliance item this section is assigned to
    address (via `compliance_items_addressed_json`), verify the draft
    actually mentions a majority of the requirement's salient terms.

    Returns a list of MAJOR/compliance_gap findings — empty when every
    addressed requirement is reasonably covered.

    Skips:
    - Sections without a draft (`draft_text_markdown is None`).
    - Requirements whose salient-term count is below
      `_MIN_SALIENT_TERMS` after stopword/length filtering — the
      missing-ratio is too noisy on tiny samples.
    - Requirement IDs that don't exist in the proposal's matrix
      (logged as a warning and skipped).
    """
    findings: list[ReviewerFindingDraft] = []

    with session_scope() as db:
        sec = db.get(ProposalSection, section_pk)
        if sec is None or not sec.draft_text_markdown:
            return findings

        addressed_ids = list(sec.compliance_items_addressed_json or [])
        if not addressed_ids:
            return findings

        # Pre-fetch every addressed requirement in one query so we don't
        # do N round-trips. Constrain by proposal_id so collisions on
        # short requirement_ids ("REQ-1") across proposals can't pull
        # the wrong row.
        # Active rows only — after an amendment supersedes a requirement,
        # two rows share the same requirement_id and the dict-comp below
        # would non-deterministically keep one; we must always use the
        # current text for keyword scanning.
        items = (
            db.execute(
                select(ComplianceMatrixItem).where(
                    ComplianceMatrixItem.proposal_id == sec.proposal_id,
                    ComplianceMatrixItem.requirement_id.in_(addressed_ids),
                    ComplianceMatrixItem.status == "active",
                )
            )
            .scalars()
            .all()
        )
        items_by_id = {it.requirement_id: it for it in items}

        # Lowercase the draft once for substring matching.
        draft_lower = (sec.draft_text_markdown or "").lower()

        for req_id in addressed_ids:
            item = items_by_id.get(req_id)
            if item is None:
                log.warning(
                    "compliance_coverage: section pk=%d references "
                    "requirement_id=%r which doesn't exist in proposal "
                    "%d's matrix — skipping.",
                    section_pk,
                    req_id,
                    sec.proposal_id,
                )
                continue

            req_text = item.requirement_text or ""
            salient = _salient_terms(req_text)

            # Sample-size gate — short requirements like "Comply with
            # FAR 52.219-9" can't be coverage-checked meaningfully.
            if len(salient) < _MIN_SALIENT_TERMS:
                continue

            missing = [t for t in salient if t not in draft_lower]
            ratio = len(missing) / len(salient)

            if ratio <= _MISSING_RATIO_THRESHOLD:
                continue

            preview = ", ".join(missing[:_FINDING_TERM_PREVIEW])
            req_excerpt = req_text.strip()
            if len(req_excerpt) > 200:
                req_excerpt = req_excerpt[:200].rstrip() + "…"

            findings.append(
                ReviewerFindingDraft(
                    severity="MAJOR",
                    category="compliance_gap",
                    finding_text=(
                        f"Section assigned to address {req_id} but the "
                        f"draft doesn't mention {len(missing)} of "
                        f"{len(salient)} salient terms from the "
                        f"requirement: {preview}. (Pre-flight keyword scan; "
                        f"if the section addresses the requirement using "
                        f"different vocabulary, dismiss this finding with "
                        f"a reason — that calibrates future runs.)"
                    ),
                    suggested_fix=(
                        f'Add content addressing {req_id}: "{req_excerpt}". '
                        f"OR if the requirement is genuinely covered "
                        f"elsewhere in the proposal, regenerate the outline "
                        f"to reassign this requirement to that section."
                    ),
                )
            )

    return findings


def check_section_credentials_allowlisted(section_pk: int) -> list[ReviewerFindingDraft]:
    """Pre-flight: flag high-risk credentials claimed in a draft when
    they are not present in company_profile.certifications.
    """
    findings: list[ReviewerFindingDraft] = []

    with session_scope() as db:
        sec = db.get(ProposalSection, section_pk)
        if sec is None or not sec.draft_text_markdown:
            return findings
        draft_lower = (sec.draft_text_markdown or "").lower()

    profile = get_company_profile()
    certifications = [str(c) for c in (profile.get("certifications") or [])]
    certs_lower = [c.lower() for c in certifications]
    held_allowlist = ", ".join(certifications) if certifications else "(none)"

    for keyword in _CREDENTIAL_KEYWORDS:
        if keyword not in draft_lower:
            continue
        if any(keyword in cert for cert in certs_lower):
            continue
        findings.append(
            ReviewerFindingDraft(
                severity="CRITICAL",
                category="hallucination",
                finding_text=(
                    f"Draft mentions credential keyword '{keyword}', but it is "
                    f"not in Quadratic's held-cert allowlist: {held_allowlist}."
                ),
                suggested_fix=(
                    "Use the assigned gap mitigation language for this credential "
                    "or replace the claim with [NEEDS_HUMAN: confirm "
                    f"{keyword} attestation status]."
                ),
            )
        )

    return findings


__all__ = [
    "check_compliance_coverage",
    "check_section_credentials_allowlisted",
]
