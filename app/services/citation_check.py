"""Deterministic pre-flight citation legitimacy check.

Reviewer A's #1 priority is catching past-performance claims sourced to
non-citable KB documents — a FAR-actionable misrepresentation if it
ships. The LLM gets it right almost every time, but "almost" isn't good
enough for federal procurement: a regex + DB lookup catches the bug
class with zero false negatives.

Runs BEFORE Reviewer A on every section. Findings are persisted with
reviewer_agent='A' (same code path as the LLM's findings) so the
auto-loop's normal flow picks them up — auto-accept CRITICAL, surface
in the directive to the writer, regenerate.

Two checks per citation:
1. Citation marker references a KB doc ID that doesn't exist → CRITICAL
   hallucination.
2. Citation source's KB class is in {prior_proposal_won, _pending, _lost}
   → CRITICAL uncited_claim regardless of claim wording. Prior proposals
   are voice-grounding only; citing them as completed work is
   FAR-actionable misrepresentation.
3. Claim looks like a past-performance assertion ("Quadratic delivered
   …") AND source class is NOT in PAST_PERFORMANCE_CITABLE_CLASSES →
   CRITICAL uncited_claim.

Citations sourced to `company_profile.*` (or any non-"KB DOC #N" string)
are treated as trustworthy by construction and skipped — the profile is
the canonical source of truth and Reviewer A handles claim/profile
mismatches separately.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from app.agents.reviewer_a import ReviewerFindingDraft
from app.core.enums import PAST_PERFORMANCE_CITABLE_CLASSES, KbDocumentClass
from app.db.session import session_scope
from app.models import KnowledgeBaseDocument, ProposalSection

log = logging.getLogger(__name__)


# Matches the writer's source_kb_doc convention: "KB DOC #14 — …".
_KB_DOC_ID_RE = re.compile(r"KB\s*DOC\s*#\s*(\d+)", re.IGNORECASE)

# Active-voice past-tense pattern with Quadratic as the SUBJECT.
#
# The naive "Quadratic + past-tense verb anywhere" check was too loose —
# it misfired on capability claims like "Quadratic's platform is built
# on NIST" and "Quadratic's policy commits to ... all delivered
# products" where past-tense verbs appear as participial adjectives or
# in passive constructions, not as actual completed-work assertions.
#
# This pattern requires Quadratic itself (or "Quadratic Digital" /
# "Quadratic's team / staff / engineers") to be the direct subject of
# the past-tense verb, with at most an optional auxiliary "has/have"
# and/or an "-ly" adverb between them. Active voice only — passive
# constructions ("Quadratic is built", "Quadratic's COOP was
# validated") and participial-adjective constructions ("the delivered
# platform", "purpose-built modules") do not match.
_PAST_PERF_VERBS = (
    "delivered",
    "completed",
    "built",
    "operated",
    "modernized",
    "ran",
    "supported",
    "led",
    "served",
    "deployed",
    "implemented",
    "integrated",
    "designed",
    "developed",
    "migrated",
    "performed",
)
_ACTIVE_PAST_PERF_RE = re.compile(
    # Subject: "Quadratic", optionally "Quadratic Digital", or
    # "Quadratic's team / staff / engineers".
    r"\bQuadratic(?:\s+Digital)?(?:'s\s+(?:team|staff|engineers|developers))?\s+"
    # Optional auxiliary for active perfect tense ("has delivered").
    r"(?:has\s+|have\s+)?"
    # Optional "-ly" adverb ("successfully delivered").
    r"(?:\w+ly\s+)?"
    # The verb itself, word-boundary anchored.
    r"\b(?:" + "|".join(_PAST_PERF_VERBS) + r")\b",
    re.IGNORECASE,
)

# Pre-compute string-valued sets so the comparison works whether SQLAlchemy
# hands us back a KbDocumentClass enum member or its raw string value.
_PRIOR_PROPOSAL_VALUES = frozenset(
    {
        KbDocumentClass.PRIOR_PROPOSAL_WON.value,
        KbDocumentClass.PRIOR_PROPOSAL_PENDING.value,
        KbDocumentClass.PRIOR_PROPOSAL_LOST.value,
    }
)
_PAST_PERF_CITABLE_VALUES = frozenset(c.value for c in PAST_PERFORMANCE_CITABLE_CLASSES)


# Stopwords for the claim-grounded-in-doc verifier (Check 4). Aggressive
# because the verifier needs DISTINCTIVE vocabulary overlap, not generic
# procurement filler. Procurement / completed-work verbs are stripped so
# a doc full of generic past-tense filler can't accidentally "match" a
# claim that has nothing else in common with it.
_CLAIM_VERIFIER_STOPWORDS = frozenset(
    {
        "with",
        "from",
        "this",
        "that",
        "they",
        "them",
        "their",
        "have",
        "will",
        "been",
        "were",
        "what",
        "when",
        "which",
        "would",
        "could",
        "should",
        "into",
        "than",
        "more",
        "also",
        "such",
        "only",
        "very",
        "even",
        "other",
        # Procurement filler — appears in nearly every doc; zero signal value
        "section",
        "include",
        "provide",
        "support",
        "service",
        "services",
        "system",
        "systems",
        "delivery",
        "approach",
        "solution",
        "solutions",
        "client",
        "customer",
        "vendor",
        "team",
        "project",
        "projects",
        "agency",
        "agencies",
        "contract",
        "contracts",
        "proposal",
        "proposals",
        "offer",
        "rfp",
        "rfps",
        "company",
        "companies",
        "business",
        # Past-perf verbs — very common in past_performance_* docs by
        # construction; matching them is no signal of grounding
        "delivered",
        "completed",
        "implemented",
        "deployed",
        "performed",
        "built",
        "operated",
        "supported",
        "served",
        "designed",
        "developed",
        "migrated",
        "integrated",
        "modernized",
        # Quadratic-side filler — same string in lots of profile/KB docs
        "quadratic",
        "digital",
    }
)

# Token regex same shape as elsewhere in the codebase. Length filter is
# applied after extraction so we keep proper-noun fragments + numerics.
_CLAIM_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _claim_content_tokens(text: str) -> set[str]:
    """Distinctive content tokens ≥ 4 chars, lowercase, stopworded.
    Returns a set so the caller can do cheap overlap math."""
    if not text:
        return set()
    out: set[str] = set()
    for m in _CLAIM_TOKEN_RE.finditer(text.lower()):
        t = m.group(0)
        if len(t) >= 4 and t not in _CLAIM_VERIFIER_STOPWORDS:
            out.add(t)
    return out


def _claim_grounded_in_doc(
    claim: str,
    doc_text: str,
    *,
    min_overlap: float = 0.5,
    min_tokens: int = 5,
) -> tuple[bool, float, int]:
    """Check whether a citation's paraphrase has enough distinctive-
    vocabulary overlap with the cited doc to be plausibly grounded.

    Returns (passed, overlap_ratio, n_distinctive_tokens). `passed` is
    True when the claim has at least `min_overlap` of its distinctive
    tokens present in the doc. Short claims (< `min_tokens` distinctive
    tokens) auto-pass — the overlap signal is too noisy on tiny inputs
    and we'd rather miss flagging than false-positive on a legitimate
    one-line citation.

    The overlap is bag-of-words on tokens ≥ 4 chars after stopword
    removal. Punctuation, casing, and word order don't matter — that
    matches the spec of "is this claim's content REPRESENTED in the
    cited doc," not "is it a verbatim sub-string."
    """
    claim_tokens = _claim_content_tokens(claim)
    if len(claim_tokens) < min_tokens:
        # Too short to score reliably — auto-pass.
        return (True, 1.0, len(claim_tokens))
    doc_tokens = _claim_content_tokens(doc_text)
    if not doc_tokens:
        return (False, 0.0, len(claim_tokens))
    matched = claim_tokens & doc_tokens
    ratio = len(matched) / len(claim_tokens)
    return (ratio >= min_overlap, ratio, len(claim_tokens))


def _doc_class_value(doc: KnowledgeBaseDocument) -> str:
    """Coerce document_class to its string value regardless of whether
    SQLAlchemy returned an enum member or a raw string."""
    cls = doc.document_class
    return cls.value if hasattr(cls, "value") else str(cls)


def _looks_like_past_perf_claim(claim: str) -> bool:
    """Heuristic: claim asserts Quadratic itself completed work.

    Requires an active-voice subject-verb match where Quadratic (or one
    of its named substitutes) is the SUBJECT of a past-tense action
    verb, optionally with "has" / "have" / an "-ly" adverb in between.

    Deliberately misses passive-voice constructions ("Quadratic's
    platform is built…", "Quadratic's COOP was validated…") and
    participial-adjective forms ("the delivered platform",
    "purpose-built modules") because those are capability framing, not
    completed-work assertions — flagging them produced too many false
    positives during smoke testing on the CRM RFI test bed.
    """
    if not claim:
        return False
    return bool(_ACTIVE_PAST_PERF_RE.search(claim))


def check_section_citations(section_pk: int) -> list[ReviewerFindingDraft]:
    """Run the deterministic citation pre-flight on one drafted section.

    Returns a list of ReviewerFindingDraft (matching reviewer_a's shape)
    that the caller persists with reviewer_agent='A'. Empty list when
    every citation is legitimate.
    """
    findings: list[ReviewerFindingDraft] = []

    with session_scope() as db:
        sec = db.get(ProposalSection, section_pk)
        if sec is None:
            log.warning(
                "citation_check: section pk=%d not found",
                section_pk,
            )
            return findings

        citations = list(sec.citations_json or [])
        if not citations:
            return findings

        # Pre-fetch every referenced KB doc in one query so we don't do
        # N round-trips for a typical 5-15-citation section.
        referenced_ids: set[int] = set()
        for c in citations:
            src = c.get("source_kb_doc") or ""
            m = _KB_DOC_ID_RE.search(src)
            if m:
                referenced_ids.add(int(m.group(1)))

        kb_lookup: dict[int, KnowledgeBaseDocument] = {}
        if referenced_ids:
            rows = (
                db.execute(select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.id.in_(referenced_ids)))
                .scalars()
                .all()
            )
            kb_lookup = {r.id: r for r in rows}

        for c in citations:
            marker = str(c.get("marker") or "?")
            claim = str(c.get("claim") or "")
            source_kb_doc = str(c.get("source_kb_doc") or "")

            m = _KB_DOC_ID_RE.search(source_kb_doc)
            if not m:
                # Profile-grounded ("company_profile.past_performance"),
                # named-section grounding, or any other non-KB-DOC source
                # — skip. These are trustworthy by construction; if the
                # writer fabricated a profile field that doesn't exist,
                # Reviewer A's hallucination check catches it.
                continue

            kb_id = int(m.group(1))
            doc = kb_lookup.get(kb_id)

            # --- Check 1: cited KB doc doesn't exist -----------------
            if doc is None:
                findings.append(
                    ReviewerFindingDraft(
                        severity="CRITICAL",
                        category="hallucination",
                        finding_text=(
                            f"Citation marker [^{marker}] points to KB DOC "
                            f"#{kb_id}, which does not exist in the knowledge "
                            f"base. The writer may have invented this source. "
                            f'Claim: "{claim}"'
                        ),
                        suggested_fix=(
                            "Either (a) replace the citation with a real KB "
                            "DOC ID that supports the claim — verify against "
                            "the KB context block in the writer's input — or "
                            "(b) rewrite the sentence to remove the "
                            "unprovable claim entirely. Do NOT renumber the "
                            "marker without re-grounding the claim."
                        ),
                    )
                )
                continue

            doc_class_value = _doc_class_value(doc)

            # --- Check 2: prior_proposal_* used as past-performance --
            # These are NEVER citable as completed work, regardless of the
            # claim wording. Even a hedge phrasing like "Quadratic has
            # experience with X" is wrong here — the doc itself isn't
            # evidence of completed work.
            if doc_class_value in _PRIOR_PROPOSAL_VALUES:
                findings.append(
                    ReviewerFindingDraft(
                        severity="CRITICAL",
                        category="uncited_claim",
                        finding_text=(
                            f"Citation marker [^{marker}] points to KB DOC "
                            f"#{kb_id} ({doc.filename}) which has class "
                            f"'{doc_class_value}'. Prior proposals "
                            f"(won/pending/lost) provide voice grounding only "
                            f"and are NEVER citable as completed work — "
                            f"citing them as past performance is FAR-"
                            f"actionable misrepresentation. "
                            f'Claim: "{claim}"'
                        ),
                        suggested_fix=(
                            "Either (a) replace the citation with a KB doc "
                            "of class past_performance_won or "
                            "past_performance_subbed that supports this "
                            "specific claim, or (b) rewrite the claim to "
                            "not assert completed work (e.g., soften to "
                            "'Quadratic can deliver…' or 'Quadratic is "
                            "positioned to…')."
                        ),
                    )
                )
                continue

            # --- Check 3: past-perf-style claim, non-citable source ---
            # Only fires when the source is a non-prior-proposal class
            # (those are caught above) AND not a citable past-perf class
            # AND the claim itself looks like a completed-work assertion.
            past_perf_misuse = False
            if doc_class_value not in _PAST_PERF_CITABLE_VALUES and _looks_like_past_perf_claim(claim):
                past_perf_misuse = True
                findings.append(
                    ReviewerFindingDraft(
                        severity="CRITICAL",
                        category="uncited_claim",
                        finding_text=(
                            f"Citation marker [^{marker}] supports a past-"
                            f"performance-style claim about Quadratic, but "
                            f"is sourced to KB DOC #{kb_id} ({doc.filename}) "
                            f"which has class '{doc_class_value}' — not "
                            f"past_performance_won/subbed. Past-performance "
                            f"citations must trace to past_performance_* "
                            f"docs or company_profile.past_performance. "
                            f'Claim: "{claim}"'
                        ),
                        suggested_fix=(
                            "Either (a) replace with a citation to a KB "
                            "doc of class past_performance_won or "
                            "past_performance_subbed that documents the "
                            "claimed work, or (b) rewrite the claim to not "
                            "assert completed work — for capability "
                            "framing, use 'Quadratic can deliver…' / "
                            "'Quadratic is positioned to…' instead of "
                            "'Quadratic delivered…'."
                        ),
                    )
                )

            # --- Check 4: claim text grounded in the cited doc -------
            # Verify the paraphrase actually has substantial vocabulary
            # overlap with the doc's text. Catches the "model said this
            # came from KB DOC #14 but the claim's content isn't in
            # there" failure mode — pure substring/token-overlap, no
            # LLM call. Skipped when Check 3 already fired (no need to
            # pile findings on the same citation) and on too-short
            # claims (the bag-of-words signal is too noisy < 5 tokens).
            if past_perf_misuse:
                continue
            passed, overlap, _n_tok = _claim_grounded_in_doc(
                claim,
                doc.extracted_text_md or "",
            )
            if not passed:
                findings.append(
                    ReviewerFindingDraft(
                        severity="MAJOR",
                        category="uncited_claim",
                        finding_text=(
                            f"Citation marker [^{marker}] points to KB DOC "
                            f"#{kb_id} ({doc.filename}), but the claim "
                            f"shares only {overlap:.0%} of its distinctive "
                            f"vocabulary with that document. The cited "
                            f"source may not actually support the claim. "
                            f'Claim: "{claim}"'
                        ),
                        suggested_fix=(
                            "Read the cited doc and confirm it contains "
                            "the claimed evidence. If it does, dismiss this "
                            "finding (the verifier is conservative — short "
                            "or paraphrased claims sometimes score low). If "
                            "it doesn't, replace the citation with one that "
                            "does support the claim, or rewrite the "
                            "sentence to drop the unsupported assertion."
                        ),
                    )
                )

    return findings
