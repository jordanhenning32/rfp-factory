"""KB context builders for agents that need retrieval.

Two flavors:
  - `build_shortfall_kb_context()`: wholesale dump of every citable KB
    document, used by Shortfall Strategist (and historically Writer
    Team). Caps at 250K chars total.
  - `build_section_kb_context()`: per-section scoped retrieval used by
    Writer Team. Filters citable KB docs to those whose text actually
    pertains to the section being drafted, plus class-based auto-
    includes (e.g., past_performance_* docs always go to a
    "Past Performance" section regardless of keyword match). Returns a
    much smaller blob — typical ~15-20K chars vs the wholesale ~158K —
    which lets us yank KB out of the Writer Team's cached prefix and
    pay only the focused subset per call.

Per design doc §7.1 (citation legitimacy): both functions only emit
content from CITABLE classes — pulling prior_proposal_* would risk the
model inferring "we delivered X" from a proposal that hasn't actually
delivered anything.
"""

from __future__ import annotations

import logging
import re

from app.core.enums import KbDocumentClass
from app.db.session import session_scope
from app.models import KnowledgeBaseDocument

log = logging.getLogger(__name__)


# Per design doc §7.1 — these are the classes where capability and past-
# performance claims can legitimately ground.
SHORTFALL_CITABLE_CLASSES: frozenset[str] = frozenset(
    {
        KbDocumentClass.CORPORATE.value,
        KbDocumentClass.PERSONNEL.value,
        KbDocumentClass.PAST_PERFORMANCE_WON.value,
        KbDocumentClass.PAST_PERFORMANCE_SUBBED.value,
        KbDocumentClass.REFERENCES_PROJECT.value,
        KbDocumentClass.REFERENCES_PERSONNEL.value,
        KbDocumentClass.COMPLIANCE_EVIDENCE.value,
    }
)


def build_shortfall_kb_context(
    *,
    max_chars_per_doc: int = 8_000,
    max_total_chars: int = 250_000,
) -> str:
    """Concatenate KB documents from citable classes into a structured context
    string for the Shortfall Strategist.

    Per-doc cap keeps any single huge doc (e.g., a 100K-char lease PDF) from
    blowing the budget. Total cap is the hard ceiling — at the default 250K
    chars (~62K tokens), there's plenty of headroom inside Sonnet's 200K
    context for profile + per-batch requirements + tool spec + system prompt.
    """
    with session_scope() as db:
        docs = (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.document_class.in_(SHORTFALL_CITABLE_CLASSES))
            .filter(KnowledgeBaseDocument.extracted_text_md.isnot(None))
            .order_by(
                KnowledgeBaseDocument.document_class,
                KnowledgeBaseDocument.id,
            )
            .all()
        )
        # Pull primitives out of the session so we can release before joining.
        items = [
            {
                "id": d.id,
                "filename": d.filename,
                "cls": d.document_class.value
                if hasattr(d.document_class, "value")
                else str(d.document_class),
                "tags": list(d.tags_json or []),
                "text": (d.extracted_text_md or "")[:max_chars_per_doc],
            }
            for d in docs
        ]

    parts: list[str] = []
    total = 0
    omitted = 0
    for i, item in enumerate(items):
        tag_str = ", ".join(item["tags"]) if item["tags"] else "none"
        block = (
            f"\n--- KB DOC #{item['id']} [{item['cls']}] {item['filename']}  "
            f"(tags: {tag_str}) ---\n"
            f"{item['text']}\n"
        )
        if total + len(block) > max_total_chars:
            omitted = len(items) - i
            break
        parts.append(block)
        total += len(block)

    if omitted:
        parts.append(
            f"\n--- ({omitted} additional KB doc(s) omitted to stay within "
            f"context budget of {max_total_chars} chars) ---\n"
        )

    log.info(
        "shortfall KB context: %d docs included (%d omitted), %d chars total",
        len(items) - omitted,
        omitted,
        total,
    )
    return "".join(parts)


# ---- Per-section KB retrieval (Writer Team) -----------------------------

# Tokenization lifted in spirit from app.services.rfp_retrieval — same
# pattern: lowercase alphanumeric tokens of length > 2, with common
# procurement filler removed. Duplicated here rather than imported to
# keep the two retrievers independent.
_KB_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_KB_STOPWORDS = frozenset(
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
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
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
        "all",
        "any",
        "can",
        "do",
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
        "also",
        "rfp",
        "vendor",
        "vendors",
        "offer",
        "offeror",
        "proposal",
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

# Class-level auto-include heuristic. If the section's title/brief
# matches one of these intent keywords, every doc of the corresponding
# KB class goes in regardless of keyword match. Catches cases the
# tokenizer misses (e.g., a "Cover Letter" section asks for corporate
# voice but the cover-letter doc may have no keyword overlap).
_SECTION_INTENT_TO_CLASSES: tuple[tuple[re.Pattern, frozenset[str]], ...] = (
    (
        re.compile(r"\bpast performance\b|\bpast experience\b|\breference|\bclient list\b", re.IGNORECASE),
        frozenset(
            {
                KbDocumentClass.PAST_PERFORMANCE_WON.value,
                KbDocumentClass.PAST_PERFORMANCE_SUBBED.value,
                KbDocumentClass.REFERENCES_PROJECT.value,
            }
        ),
    ),
    (
        re.compile(r"\bpersonnel\b|\bstaffing\b|\bkey staff\b|\bteam composition\b|\bresume", re.IGNORECASE),
        frozenset(
            {
                KbDocumentClass.PERSONNEL.value,
                KbDocumentClass.REFERENCES_PERSONNEL.value,
            }
        ),
    ),
    (
        re.compile(
            r"\bcompany background\b|\bcompany overview\b|\bcorporate\b|\bcover letter\b|\bexecutive summary\b",
            re.IGNORECASE,
        ),
        frozenset({KbDocumentClass.CORPORATE.value}),
    ),
    (
        re.compile(
            r"\bsecurity\b|\bcompliance\b|\bcertif|\bSOC ?2\b|\bFedRAMP\b|\bISO 27001\b|\bNIST\b",
            re.IGNORECASE,
        ),
        frozenset({KbDocumentClass.COMPLIANCE_EVIDENCE.value}),
    ),
)


def _kb_tokenize(text: str) -> list[str]:
    return [
        t
        for t in (m.group(0).lower() for m in _KB_TOKEN_RE.finditer(text))
        if len(t) > 2 and t not in _KB_STOPWORDS
    ]


def _kb_query_terms_from(strings: list[str]) -> set[str]:
    out: set[str] = set()
    for s in strings:
        if not s:
            continue
        out.update(_kb_tokenize(s))
    return out


def _kb_score(doc_text: str, query_terms: set[str]) -> tuple[int, int]:
    """(distinct_terms_present, total_term_count) — same shape as the
    RFP retriever for consistency. Distinct count rewards docs that
    cover MORE of the section's facets; total count is the tiebreaker."""
    if not query_terms:
        return (0, 0)
    tokens = _kb_tokenize(doc_text)
    if not tokens:
        return (0, 0)
    distinct, total, seen = 0, 0, set()
    for tok in tokens:
        if tok in query_terms:
            total += 1
            if tok not in seen:
                seen.add(tok)
                distinct += 1
    return (distinct, total)


def build_section_kb_context(
    *,
    section_title: str,
    section_brief: str,
    compliance_item_texts: list[str] | None = None,
    max_chars_per_doc: int = 8_000,
    max_total_chars: int = 25_000,
) -> str:
    """Per-section KB context for the Writer Team. Returns a string in
    the same format as build_shortfall_kb_context (one block per doc)
    but scoped to docs that actually pertain to this section.

    Selection: union of class-auto-includes (driven by section-intent
    regexes — past-performance sections get every PP doc, personnel
    sections get every staff doc, etc.) plus keyword-scored docs from
    the rest of the citable corpus. Auto-includes go in unconditionally;
    scored docs fill the remaining budget by descending relevance.

    Returns empty string when no KB docs match — caller should treat
    that as "no scoped KB available; the writer can lean on the company
    profile alone for this section."
    """
    query_terms = _kb_query_terms_from([section_title, section_brief, *(compliance_item_texts or [])])
    intent_match = ", ".join(
        [
            "/".join(sorted(classes))
            for pat, classes in _SECTION_INTENT_TO_CLASSES
            if pat.search(f"{section_title} {section_brief}")
        ]
    )

    auto_classes: set[str] = set()
    for pat, classes in _SECTION_INTENT_TO_CLASSES:
        if pat.search(f"{section_title} {section_brief}"):
            auto_classes.update(classes)

    with session_scope() as db:
        docs = (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.document_class.in_(SHORTFALL_CITABLE_CLASSES))
            .filter(KnowledgeBaseDocument.extracted_text_md.isnot(None))
            .order_by(
                KnowledgeBaseDocument.document_class,
                KnowledgeBaseDocument.id,
            )
            .all()
        )
        items = [
            {
                "id": d.id,
                "filename": d.filename,
                "cls": d.document_class.value
                if hasattr(d.document_class, "value")
                else str(d.document_class),
                "tags": list(d.tags_json or []),
                "text": (d.extracted_text_md or "")[:max_chars_per_doc],
            }
            for d in docs
        ]

    # Score every doc — even ones in auto-include classes — so we can
    # rank within the auto set when the budget is tight. Auto-class docs
    # get a large priority boost on the distinct-term axis (so they
    # outrank non-auto docs at equal relevance) but still respect the
    # total budget.
    _AUTO_PRIORITY_BOOST = 1_000
    scored_items: list[tuple[int, int, dict]] = []
    for it in items:
        distinct, total = _kb_score(it["text"], query_terms)
        is_auto = it["cls"] in auto_classes
        if not is_auto and distinct == 0:
            # Off-topic: not in an auto class AND no keyword overlap.
            continue
        priority = distinct + (_AUTO_PRIORITY_BOOST if is_auto else 0)
        scored_items.append((priority, total, it))

    scored_items.sort(key=lambda t: (-t[0], -t[1], t[2]["id"]))

    # Greedy fill, single budget. Auto-class docs lead because their
    # priority is boosted; once the budget is exhausted the rest drop
    # — including auto docs that didn't fit. Avoids the previous bug
    # where every "cover letter"-style section dragged in 150k chars
    # of corporate boilerplate regardless of budget.
    chosen: list[dict] = []
    used = 0
    n_auto = 0
    for _priority, _total, it in scored_items:
        cost = _kb_block_size(it)
        if used + cost > max_total_chars:
            continue
        chosen.append(it)
        used += cost
        if it["cls"] in auto_classes:
            n_auto += 1

    if not chosen:
        log.info(
            "section KB context: 0 docs matched section_title=%r (intent=%r, query_terms=%d) — empty context",
            section_title,
            intent_match or "none",
            len(query_terms),
        )
        return ""

    parts: list[str] = []
    for it in chosen:
        tag_str = ", ".join(it["tags"]) if it["tags"] else "none"
        parts.append(
            f"\n--- KB DOC #{it['id']} [{it['cls']}] {it['filename']}  (tags: {tag_str}) ---\n{it['text']}\n"
        )

    log.info(
        "section KB context for %r: %d docs included (intent=%r, auto=%d, scored=%d), %d chars",
        section_title,
        len(chosen),
        intent_match or "none",
        n_auto,
        len(chosen) - n_auto,
        used,
    )
    return "".join(parts)


def _kb_block_size(item: dict) -> int:
    """Approx the block size built per doc (mirrors the format above)
    so the budget accounting stays accurate without rendering twice."""
    tag_str = ", ".join(item["tags"]) if item["tags"] else "none"
    header = f"\n--- KB DOC #{item['id']} [{item['cls']}] {item['filename']}  (tags: {tag_str}) ---\n"
    return len(header) + len(item["text"]) + 1
