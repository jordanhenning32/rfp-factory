"""Per-section RFP excerpt retrieval.

The Writer Team historically received the entire RFP text (~170k chars
on a 56-page PDF) baked into its cached prefix. That bloated the prefix
to ~40k tokens and most of the text was irrelevant to any single
section. This module builds a focused excerpt per section — a few
thousand chars of "the bits of the RFP this section actually needs" —
which the writer renders in the user prompt instead of the cached
prefix. Net effect: cheaper prefill (the shared cache is now ~30%
smaller) and tighter focus.

Approach: paragraph-level term-frequency scoring with no extra deps.
Auto-include paragraphs that mention universal context (Section L/M,
Evaluation Criteria, Instructions to Offerors, Scope of Work) since
those govern every section's framing whether or not their wording
overlaps the section_brief.

Not LLM-backed. Pure regex + dict-counter over UTF-8 text.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


# Default budget per section. Tuned for cost: a typical 60-120k-char
# RFP returns ~5-12 high-relevance paragraphs at this cap, plus the
# auto-include set (Section L/M, Evaluation Criteria, etc.). Going
# higher just appends progressively-weaker matches that pay full
# user-prompt input rate (uncached). Going much lower starts dropping
# real content. 8k is the empirical sweet spot — enough to anchor a
# section's narrative in source language, small enough that 9 sections
# × 8k still totals less than the full RFP we used to send in the
# cached prefix.
DEFAULT_MAX_CHARS = 8_000

# Paragraphs shorter than this are usually headers / list bullets / page
# numbers — keep them only if they're inside the auto-include patterns.
_MIN_PARAGRAPH_CHARS = 80

# Patterns that mark a chunk as PDF-extraction noise — drop before
# scoring. The dot-leader run (10+ consecutive dots) is the dead
# giveaway for a table-of-contents entry; TOC content lexically
# matches everything but adds zero useful context for the writer.
_NOISE_RES = (re.compile(r"\.{10,}"),)


def _is_noise(paragraph: str) -> bool:
    return any(pat.search(paragraph) for pat in _NOISE_RES)


# Stopwords for query term extraction. Aggressively short list — RFP
# vocabulary is usually domain-specific enough that even "service" /
# "project" carry signal. Just drop the most-common English filler.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "his",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "she",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "will",
        "with",
        "your",
        "you",
        "i",
        "if",
        "all",
        "any",
        "can",
        "do",
        "does",
        "etc",
        "every",
        "into",
        "may",
        "must",
        "no",
        "not",
        "so",
        "some",
        "such",
        "than",
        "then",
        "there",
        "these",
        "those",
        "what",
        "when",
        "which",
        "who",
        "why",
        "how",
        "our",
        "out",
        "also",  # Procurement filler that appears in every RFP & gives no signal:
        "rfp",
        "vendor",
        "vendors",
        "offer",
        "offeror",
        "offerors",
        "proposal",
        "proposals",
        "section",
        "sections",
        "shall",
        "should",
        "describe",
        "provide",
        "include",
        "submit",
        "submission",
    }
)

# Regex patterns that auto-include a paragraph regardless of TF score.
# These mark RFP sections that govern every drafted section's framing.
# Case-sensitive on the all-caps phrases (RFPs reliably uppercase
# governance headings); case-insensitive on FAR-style "Section L/M"
# tokens to catch both styles.
_AUTO_INCLUDE_RES = (
    re.compile(r"\bSECTION L\b|\bSECTION M\b", re.IGNORECASE),
    re.compile(r"\bINSTRUCTIONS TO OFFERORS?\b"),
    re.compile(r"\bEVALUATION CRITERI(?:A|ON)\b"),
    re.compile(r"\bEVALUATION FACTORS?\b"),
    re.compile(r"\bSCOPE OF WORK\b|\bSTATEMENT OF WORK\b"),
    re.compile(r"\bPROPOSAL FORMAT\b|\bPROPOSAL ORGANIZATION\b"),
    re.compile(r"\bSCORING METHODOLOGY\b"),
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, length > 2, with stopwords removed.
    Used for both query term extraction and per-paragraph counting."""
    return [
        t
        for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text))
        if len(t) > 2 and t not in _STOPWORDS
    ]


# Target chunk size for the sentence-chunker. RFP PDFs from pdfplumber
# have aggressive soft-wrapping (one line per ~80 chars regardless of
# semantic boundaries) and very few blank-line paragraph separators,
# so we can't rely on natural paragraph structure. Instead, flatten
# whitespace and re-chunk at sentence boundaries into ~target_chars-
# size pieces. Big enough to carry a coherent thought; small enough
# that a 60k-char RFP yields ~80-100 scoring units (good resolution).
_CHUNK_TARGET_CHARS = 700

# Sentence-splitter regex: split AFTER ".", "!", or "?" followed by
# whitespace. Tolerates trailing quotes / parens but avoids splitting
# on decimals (5.2) or ellipses (...). Empirically fine on RFP text;
# proper sentence segmentation would need an NLP lib we don't want.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?][\"')\]])\s+|(?<=[.!?])\s+(?=[A-Z])")


def _split_paragraphs(rfp_text: str) -> list[str]:
    """Flatten whitespace and re-chunk RFP text at sentence boundaries
    into ~_CHUNK_TARGET_CHARS pieces. PDF-extracted text rarely has
    real paragraph breaks (only ~20 blank lines in 60k chars on a
    typical RFP), so structural splitting fails. Sentence-level
    chunking gives the retrieval scorer something usable.

    Page markers ("--- Page N ---") and file markers stay embedded in
    chunks; that's fine — the noise filter drops standalone-marker
    lines via _is_noise()."""
    flat = re.sub(r"\s+", " ", rfp_text).strip()
    if not flat:
        return []
    sentences = _SENTENCE_SPLIT_RE.split(flat)
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        cost = len(sent) + 1  # +1 for the space separator
        if current and current_chars + cost > _CHUNK_TARGET_CHARS:
            chunks.append(" ".join(current))
            current = [sent]
            current_chars = len(sent)
        else:
            current.append(sent)
            current_chars += cost
    if current:
        chunks.append(" ".join(current))
    return chunks


def _is_auto_include(paragraph: str) -> bool:
    return any(pat.search(paragraph) for pat in _AUTO_INCLUDE_RES)


def _score_paragraph(paragraph: str, query_terms: set[str]) -> tuple[int, int]:
    """Return (distinct_query_terms_present, total_term_count) so we can
    rank by distinctness first (matching more *facets* of the section
    brief beats mentioning one term repeatedly), with total count as
    tiebreaker."""
    para_tokens = _tokenize(paragraph)
    if not para_tokens or not query_terms:
        return (0, 0)
    distinct = 0
    total = 0
    seen: set[str] = set()
    for tok in para_tokens:
        if tok in query_terms:
            total += 1
            if tok not in seen:
                seen.add(tok)
                distinct += 1
    return (distinct, total)


def _query_terms_from(strings: list[str]) -> set[str]:
    """Collect lowercase query tokens from the section_title, brief, and
    compliance item texts. Deduped + stopworded."""
    out: set[str] = set()
    for s in strings:
        if not s:
            continue
        out.update(_tokenize(s))
    return out


def build_section_rfp_excerpt(
    rfp_full_text: str,
    *,
    section_title: str,
    section_brief: str,
    compliance_item_texts: list[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Return the most-relevant slice of `rfp_full_text` for one
    proposal section. Concatenated paragraphs separated by double
    newlines, capped at `max_chars`.

    Always-include paragraphs (Section L/M, Evaluation Criteria,
    Instructions to Offerors, Scope of Work) come first regardless of
    score — they govern the entire proposal. Then top-scoring paragraphs
    by term-frequency match against the section's query terms fill the
    remaining budget. Empty input → empty string.
    """
    if not rfp_full_text:
        return ""

    paragraphs = _split_paragraphs(rfp_full_text)
    if not paragraphs:
        return ""

    query_terms = _query_terms_from([section_title, section_brief, *(compliance_item_texts or [])])

    # Partition: auto-include paragraphs always make the cut. Everything
    # else is scored and ranked. We track original index so the final
    # output preserves source order (helps the LLM keep the RFP's own
    # narrative coherent).
    auto: list[tuple[int, str]] = []
    scored: list[tuple[int, int, int, str]] = []
    for idx, para in enumerate(paragraphs):
        # Drop PDF noise (TOC dot-leader entries, "--- Page N ---" markers)
        # before either scoring or auto-include — these match a lot of
        # query terms incidentally and pollute the excerpt with table-of-
        # contents lines that read like garbage to the LLM.
        if _is_noise(para):
            continue
        if _is_auto_include(para):
            auto.append((idx, para))
            continue
        if len(para) < _MIN_PARAGRAPH_CHARS:
            continue
        distinct, total = _score_paragraph(para, query_terms)
        if distinct == 0:
            continue
        scored.append((distinct, total, idx, para))

    # Auto-includes go in unconditionally (deduped against scored set).
    chosen: list[tuple[int, str]] = list(auto)
    auto_idxs = {i for i, _ in auto}

    # Rank scored paragraphs by (distinct desc, total desc, index asc).
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))

    # Greedy fill up to max_chars, preserving source order in the output.
    used_chars = sum(len(p) + 2 for _, p in chosen)  # +2 for the "\n\n" separators
    for _distinct, _total, idx, para in scored:
        if idx in auto_idxs:
            continue
        cost = len(para) + 2
        if used_chars + cost > max_chars:
            continue
        chosen.append((idx, para))
        used_chars += cost

    chosen.sort(key=lambda t: t[0])
    return "\n\n".join(p for _, p in chosen)


__all__ = ["build_section_rfp_excerpt", "DEFAULT_MAX_CHARS"]
