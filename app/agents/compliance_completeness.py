"""Source-aware completeness audit for an extracted compliance matrix.

Unlike the classification validator, this reviewer sees canonical source text
and asks an independent provider to return only requirements that appear to be
missing from the current matrix.  Source units are page-aligned, failed calls
are retried at a smaller size, and a bounded Haiku fallback is explicit in the
result state.

Candidates are not trusted merely because a model returned them.  Every quote
must be present verbatim after whitespace normalization, carry a page inside
the reviewed unit, use valid enums, and not already be represented by an
extracted row.  The intake layer may auto-add only ``auto_add_eligible`` HIGH
confidence candidates.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Literal

from app.config import get_settings
from app.core.enums import RequirementCategory, RequirementType
from app.services.llm import (
    call_tool_for_model,
    fmt_llm_usage,
    is_transient_provider_error,
)

log = logging.getLogger(__name__)


_SOURCE_TARGET_CHARS = 25_000
_MIN_RETRY_CHARS = 5_000
_MAX_SPLIT_DEPTH = 3
_PAGE_MARKER_RE = re.compile(r"^---\s*Page\s+(\d+)\s*---$", re.MULTILINE | re.IGNORECASE)
_PAGE_MARKER_REMOVE_RE = re.compile(
    r"---\s*Page\s+\d+\s*---", re.IGNORECASE,
)
_VALID_TYPES = {item.value for item in RequirementType}
_VALID_CATEGORIES = {item.value for item in RequirementCategory}
_VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019/-][A-Za-z0-9]+)*")
_MODAL_RE = re.compile(r"\b(?:shall|must|should)\b", re.IGNORECASE)
_REQUIRED_TO_RE = re.compile(r"\b(?:is|are)\s+required\s+to\b", re.IGNORECASE)
_REQUIRED_RESULT_RE = re.compile(
    r"\b(?:is|are)\s+(?:mandatory|required)\b", re.IGNORECASE,
)
_PARTY_WILL_RE = re.compile(
    r"\b(?:offeror|proposer|respondent|bidder|vendor|contractor|supplier|applicant)"
    r"s?\b.{0,100}\bwill\b",
    re.IGNORECASE,
)
_IMPERATIVE_RE = re.compile(
    r"^(?:please\s+)?(?:submit|provide|attach|include|complete|sign|upload|enter|"
    r"describe|explain|identify|list|demonstrate|certify|acknowledge|return|"
    r"furnish|respond|state|indicate|document|price|address)\b",
    re.IGNORECASE,
)
_EVALUATION_RE = re.compile(
    r"\b(?:evaluat(?:e|ed|es|ion)|scor(?:e|ed|es|ing)|points?|weight(?:ed|ing)?|"
    r"award\s+factor|rating|preference)\b",
    re.IGNORECASE,
)
_FORMAT_RE = re.compile(
    r"\b(?:is|are)\s+due\b|\bmay\s+not\s+exceed\b|\bno\s+more\s+than\b|"
    r"\blimited\s+to\b",
    re.IGNORECASE,
)
_SHALL_RE = re.compile(
    r"\b(?:shall|is required to|are required to|will be required)\b",
    re.IGNORECASE,
)
_MUST_RE = re.compile(
    r"\b(?:must|is required to|are required to|will be required)\b",
    re.IGNORECASE,
)
_SHOULD_RE = re.compile(r"\bshould\b", re.IGNORECASE)
_FORM_REQUIREMENT_RE = re.compile(
    r"\b(?:submit|attach|include|complete|sign|upload|return|furnish|certify)\b"
    r".{0,120}\b(?:form|certificate|certification|attachment|resume|report|"
    r"affidavit|acknowledg(?:e|ement)|schedule)\b",
    re.IGNORECASE,
)
_SUBMISSION_FORMAT_RE = re.compile(
    r"\b(?:page\s+limit|pages?|font|margin|file\s+(?:type|format)|deadline|due\s+date|"
    r"electronic(?:ally)?|hard\s+cop(?:y|ies)|copies|megabytes?|mb\b)\b",
    re.IGNORECASE,
)
_LEADING_ENUM_RE = re.compile(
    r"^\s*(?:(?:\(?[A-Za-z0-9]+\)?[.)])|(?:[-\u2022]))\s*",
)
_LEADING_FRAGMENT_RE = re.compile(
    r"^(?:and|or|because|if|when|that|which|who|while|unless|whereas)\b",
    re.IGNORECASE,
)
_DANGLING_FRAGMENT_END_RE = re.compile(
    r"\b(?:and|or|with|to|of|for|from|including|under|against|by|at|the|a|an|"
    r"shall|must|should|will|be|is|are)\s*[,;:-]?$",
    re.IGNORECASE,
)


_TOOL: dict = {
    "name": "report_completeness_review",
    "description": (
        "Compare one source unit with the supplied extracted matrix rows. "
        "Return only source requirements that are genuinely missing, plus "
        "passages that cannot be judged safely."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "missing_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_text": {
                            "type": "string",
                            "description": (
                                "Verbatim source quotation containing the "
                                "complete missing requirement."
                            ),
                        },
                        "source_page": {"type": "integer"},
                        "source_section": {"type": "string"},
                        "requirement_type": {
                            "type": "string",
                            "enum": sorted(_VALID_TYPES),
                        },
                        "category": {
                            "type": "string",
                            "enum": sorted(_VALID_CATEGORIES),
                        },
                        "weight": {"type": "number"},
                        "confidence": {
                            "type": "string",
                            "enum": ["HIGH", "MEDIUM", "LOW"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "One short sentence explaining why it is missing.",
                        },
                    },
                    "required": [
                        "requirement_text",
                        "source_page",
                        "requirement_type",
                        "category",
                        "confidence",
                        "reason",
                    ],
                },
            },
            "uncertain_passages": {
                "type": "array",
                "description": (
                    "Passages that may contain a requirement but cannot be "
                    "classified safely from this source unit."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "source_page": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["source_page", "reason"],
                },
            },
        },
        "required": ["missing_candidates", "uncertain_passages"],
    },
}


_SYSTEM = """You are an independent RFP requirements-completeness auditor. Compare the canonical source excerpt against the current compliance-matrix rows for that document. Return ONLY mandatory requirements, submission instructions, required forms/certifications, evaluation criteria, pricing instructions, or material advisory requirements that are present in the source but absent from the matrix.

Rules:
- A candidate requirement_text must be copied verbatim and completely from the source excerpt. Never paraphrase.
- Do not return headings, definitions, background narrative, examples, contract boilerplate without an offeror/contractor obligation, or rows already represented by the current matrix.
- Parent mandatory language can govern child bullets. Include the complete child requirement with enough source wording to preserve that inherited context.
- submission_format is procedural (page limits, font, file format, deadline, signature). A substantive Describe/Explain/Provide prompt is not submission_format.
- evaluation_criterion covers scoring, weights, evaluator ratings, and award factors.
- A buyer pricing workbook is an artifact, not a list of requirements by default. Do not return blank entry cells, column labels, formulas, subtotals, or worksheet headings. Return only explicit instructions or obligations written in the workbook.
- HIGH means the passage is plainly a requirement and plainly absent. Use MEDIUM/LOW when context or table structure is ambiguous.
- If a passage cannot be judged safely, put its page and reason in uncertain_passages instead of inventing a candidate.
- A truly complete source unit returns both arrays empty.
"""


_USER_TEMPLATE = """DOCUMENT: {filename}
SOURCE UNIT: {unit_label}

CURRENT EXTRACTED MATRIX ROWS FOR THIS DOCUMENT:
{items_text}

CANONICAL SOURCE TEXT:
{source_text}
"""


CompletenessState = Literal["complete", "degraded", "partial", "failed"]


@dataclass(frozen=True)
class SourceUnit:
    index: int
    text: str
    pages: tuple[int, ...]
    label: str
    depth: int = 0


@dataclass
class MissingRequirementCandidate:
    requirement_text: str
    source_page: int
    source_section: str | None
    requirement_type: str
    category: str
    weight: float | None
    confidence: str
    reason: str
    auto_add_eligible: bool = False
    near_duplicate: bool = False
    review_role: Literal["primary", "fallback"] = "primary"


@dataclass(frozen=True)
class UncertainPassage:
    source_page: int
    reason: str


@dataclass(frozen=True)
class CompletenessAttempt:
    model: str
    role: Literal["primary", "fallback"]
    unit_label: str
    depth: int
    success: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error_kind: str | None = None


@dataclass
class ComplianceCompletenessReport:
    source_units_total: int
    primary_model: str
    fallback_model: str
    source_sha256: str
    matrix_sha256: str
    candidates: list[MissingRequirementCandidate] = field(default_factory=list)
    uncertain_passages: list[UncertainPassage] = field(default_factory=list)
    attempts: list[CompletenessAttempt] = field(default_factory=list)
    reviewed_unit_labels: list[str] = field(default_factory=list)
    unresolved_unit_labels: list[str] = field(default_factory=list)
    duplicate_candidates_ignored: int = 0

    @property
    def reviewed_units(self) -> int:
        return len(dict.fromkeys(self.reviewed_unit_labels))

    @property
    def fallback_used(self) -> bool:
        return any(a.role == "fallback" and a.success for a in self.attempts)

    @property
    def retry_used(self) -> bool:
        return any(a.role == "primary" and a.depth > 0 for a in self.attempts)

    @property
    def auto_add_candidates(self) -> list[MissingRequirementCandidate]:
        return [candidate for candidate in self.candidates if candidate.auto_add_eligible]

    @property
    def manual_review_candidates(self) -> list[MissingRequirementCandidate]:
        return [candidate for candidate in self.candidates if not candidate.auto_add_eligible]

    @property
    def state(self) -> CompletenessState:
        if self.source_units_total == 0:
            return "complete"
        if self.reviewed_units == 0:
            return "failed"
        if self.unresolved_unit_labels:
            return "partial"
        if self.fallback_used:
            return "degraded"
        return "complete"

    @property
    def input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.attempts)

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.attempts)

    def as_public_dict(self) -> dict:
        return {
            "state": self.state,
            "source_units_total": self.source_units_total,
            "reviewed_units": self.reviewed_units,
            "unresolved_unit_labels": list(self.unresolved_unit_labels),
            "primary_model": self.primary_model,
            "fallback_model": self.fallback_model,
            "primary_attempts": sum(a.role == "primary" for a in self.attempts),
            "primary_failed_attempts": sum(
                a.role == "primary" and not a.success for a in self.attempts
            ),
            "retry_used": self.retry_used,
            "fallback_used": self.fallback_used,
            "candidate_count": len(self.candidates),
            "auto_add_candidate_count": len(self.auto_add_candidates),
            "manual_review_candidate_count": len(self.manual_review_candidates),
            "uncertain_passage_count": len(self.uncertain_passages),
            "duplicate_candidates_ignored": self.duplicate_candidates_ignored,
            "source_sha256": self.source_sha256,
            "matrix_sha256": self.matrix_sha256,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.cost_usd, 6),
            "manual_review": [
                {
                    "requirement_text": item.requirement_text,
                    "source_page": item.source_page,
                    "source_section": item.source_section,
                    "requirement_type": item.requirement_type,
                    "category": item.category,
                    "weight": item.weight,
                    "confidence": item.confidence,
                    "reason": item.reason,
                    "near_duplicate": item.near_duplicate,
                    "review_role": item.review_role,
                }
                for item in self.manual_review_candidates[:25]
            ],
            "uncertain_passages": [
                {
                    "source_page": passage.source_page,
                    "reason": passage.reason,
                }
                for passage in self.uncertain_passages[:25]
            ],
        }


class CompletenessProtocolError(RuntimeError):
    """A tool payload could not prove a valid source review."""


def _normalize_text(text: str) -> str:
    without_markers = _PAGE_MARKER_REMOVE_RE.sub(" ", text or "")
    return " ".join(without_markers.split()).strip().lower()


def _meaningful_source(text: str) -> bool:
    cleaned = _PAGE_MARKER_REMOVE_RE.sub("", text or "")
    return any(char.isalnum() for char in cleaned)


_SECTION_IDENTIFIER_RE = re.compile(
    r"^(?:section\s+)?(?:[a-z](?:\.\d+)+|\d+(?:\.\d+)+)$",
    re.IGNORECASE,
)


def _source_section_is_supported(value: str, source_text: str) -> bool:
    """Verify that a supplied section is a structural line, not prose text."""

    needle = _normalize_text(value)
    if not needle:
        return False
    numbered = _SECTION_IDENTIFIER_RE.fullmatch(needle) is not None
    named = len(_WORD_RE.findall(needle)) >= 2
    if not (numbered or named):
        return False

    def requirement_like(line: str) -> bool:
        return bool(
            _MODAL_RE.search(line)
            or _REQUIRED_TO_RE.search(line)
            or _REQUIRED_RESULT_RE.search(line)
            or _PARTY_WILL_RE.search(line)
            or _IMPERATIVE_RE.search(line)
            or _FORMAT_RE.search(line)
            or (
                _EVALUATION_RE.search(line)
                and re.search(r"\b(?:will|is|are|shall|must)\b", line)
            )
        )

    for raw_line in source_text.splitlines():
        line = _normalize_text(raw_line)
        if not line or len(line) > 180:
            continue
        if numbered:
            patterns = (needle, f"section {needle}")
            if any(
                re.match(rf"^{re.escape(pattern)}(?:\s|[:.\-])", line)
                or line == pattern
                for pattern in patterns
            ):
                return True
        elif line == needle and not requirement_like(line):
            return True
        elif (
            line.startswith(needle + " ")
            and not requirement_like(line)
        ):
            return True
    return False


def _source_text_for_page(unit: SourceUnit, page: int) -> str:
    """Return only the canonical text attributed to ``page`` in a unit."""

    matches = list(_PAGE_MARKER_RE.finditer(unit.text))
    if not matches:
        return unit.text if page in set(unit.pages) else ""
    segments: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(unit.text)
        if int(match.group(1)) == page:
            # Parser preambles before the first page marker belong to the
            # first marked page. Preserve them during quote verification just
            # as `_source_units` preserves them for model review.
            start = 0 if index == 0 else match.start()
            segments.append(unit.text[start:end])
    return "".join(segments)


def _number_pattern(value: float) -> str:
    rendered = format(value, ".15g")
    if "." in rendered:
        whole, fraction = rendered.split(".", 1)
        rendered = rf"{re.escape(whole)}\.{re.escape(fraction)}0*"
    else:
        rendered = rf"{re.escape(rendered)}(?:\.0+)?"
    return rf"(?<![\d.]){rendered}(?![\d.])"


def _weight_is_supported(
    weight: float,
    quote: str,
    requirement_type: str,
) -> bool:
    """Verify a model-provided numeric weight from the quoted requirement.

    The quote, rather than an arbitrary occurrence elsewhere in the source
    unit, must carry the number.  Fractional weights may be supported by their
    percentage form (for example, ``0.6`` and ``60 percent``).
    """

    if (
        requirement_type != RequirementType.EVALUATION_CRITERION.value
        or not math.isfinite(weight)
        or weight < 0
    ):
        return False
    number = _number_pattern(weight)
    if re.search(rf"{number}\s*(?:%|percent\b|points?\b)", quote, re.IGNORECASE):
        return True
    if re.search(
        rf"\b(?:weight(?:ed)?|worth|score(?:d)?)\b\D{{0,24}}{number}",
        quote,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"{number}\D{{0,24}}\b(?:weight|weighted|points?|percent)\b",
        quote,
        re.IGNORECASE,
    ):
        return True
    if 0 < weight <= 1:
        percentage = weight * 100
        return re.search(
            rf"{_number_pattern(percentage)}\s*(?:%|percent\b)",
            quote,
            re.IGNORECASE,
        ) is not None
    return False


def _quote_is_source_boundary_aligned(quote: str, page_source: str) -> bool:
    """Require both start and end boundaries inside one canonical source line."""

    text = quote.strip()
    if text.endswith(("...", "…")):
        return False
    normalized = _normalize_text(text)
    if not normalized:
        return False
    candidate_has_terminal = re.search(r"[.!?][\"')\]]*$", text) is not None
    for raw_line in page_source.splitlines():
        line = _normalize_text(_LEADING_ENUM_RE.sub("", raw_line.strip()))
        if not line:
            continue
        start = 0
        while True:
            index = line.find(normalized, start)
            if index < 0:
                break
            before = line[:index].rstrip()
            after = line[index + len(normalized) :].lstrip()
            starts_at_boundary = not before or before.endswith((".", "!", "?"))
            ends_at_boundary = not after or candidate_has_terminal
            if starts_at_boundary and ends_at_boundary:
                return True
            start = index + 1
    return False


def _looks_like_complete_requirement(quote: str, requirement_type: str) -> bool:
    """Conservatively distinguish a full requirement from a source fragment.

    Literal source verification alone is insufficient: common words such as
    "shall" occur in many documents and are not complete requirements.  This
    gate requires a grammatical obligation, an explicit instruction, a common
    submission-format constraint, or an evaluation/scoring statement.  Items
    that fail remain visible for human review; they are never auto-added.
    """

    text = " ".join((quote or "").split()).strip()
    words = _WORD_RE.findall(text)
    # Auto-recovery is intentionally conservative.  A literal source match is
    # not enough: short spans such as "contractor shall provide" and
    # "Technical approach will be evaluated" are commonly clipped clauses.
    # Brief candidates must carry a visible sentence/list-item boundary; long
    # table-cell requirements may omit punctuation but still need enough
    # context to be independently understandable.
    if len(words) < 5 or len(text) < 20:
        return False
    unenumerated = _LEADING_ENUM_RE.sub("", text)
    if (
        _LEADING_FRAGMENT_RE.search(unenumerated)
        or _DANGLING_FRAGMENT_END_RE.search(text)
        or text.endswith(("...", "…"))
    ):
        return False
    has_terminal_boundary = re.search(r"[.!?;][\"')\]]*$", text) is not None
    if len(words) < 8 and not has_terminal_boundary:
        return False

    modal = _MODAL_RE.search(text) or _REQUIRED_TO_RE.search(text)
    if modal:
        before = _WORD_RE.findall(text[: modal.start()])
        after = _WORD_RE.findall(text[modal.end() :])
        if before and after:
            return True

    # "A signed certification is required" is complete even though the
    # obligation word closes the sentence rather than introducing an action.
    required_result = _REQUIRED_RESULT_RE.search(text)
    if required_result and len(_WORD_RE.findall(text[: required_result.start()])) >= 2:
        return True

    party_will = _PARTY_WILL_RE.search(text)
    if party_will and _WORD_RE.findall(text[party_will.end() :]):
        return True

    if _IMPERATIVE_RE.search(unenumerated):
        return True

    if requirement_type == RequirementType.SUBMISSION_FORMAT.value and _FORMAT_RE.search(text):
        return True

    return (
        requirement_type == RequirementType.EVALUATION_CRITERION.value
        and _EVALUATION_RE.search(text) is not None
        and len(words) >= 4
    )


def _requirement_type_is_supported(quote: str, requirement_type: str) -> bool:
    """Require deterministic textual evidence for auto-added type metadata."""

    if requirement_type == RequirementType.SHALL.value:
        return _SHALL_RE.search(quote) is not None
    if requirement_type == RequirementType.MUST.value:
        return _MUST_RE.search(quote) is not None
    if requirement_type == RequirementType.SHOULD.value:
        return (
            _SHOULD_RE.search(quote) is not None
            and _SHALL_RE.search(quote) is None
            and _MUST_RE.search(quote) is None
        )
    if requirement_type == RequirementType.MANDATORY_FORM.value:
        return _FORM_REQUIREMENT_RE.search(quote) is not None
    if requirement_type == RequirementType.SUBMISSION_FORMAT.value:
        return (
            _FORMAT_RE.search(quote) is not None
            or _SUBMISSION_FORMAT_RE.search(quote) is not None
        )
    if requirement_type == RequirementType.EVALUATION_CRITERION.value:
        return _EVALUATION_RE.search(quote) is not None
    return False


def _source_units(text: str, target_chars: int = _SOURCE_TARGET_CHARS) -> list[SourceUnit]:
    """Split canonical source on page boundaries, preserving page numbers."""

    matches = list(_PAGE_MARKER_RE.finditer(text or ""))
    if not matches:
        if not _meaningful_source(text):
            return []
        chunks = _split_text_approximately(text, target_chars)
        return [
            SourceUnit(index=i, text=chunk, pages=(1,), label=f"part {i + 1}/{len(chunks)}")
            for i, chunk in enumerate(chunks)
            if _meaningful_source(chunk)
        ]

    page_segments: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # Some extractors emit a title/preamble before their first canonical
        # page marker. Treat that prefix as part of the first marked page so a
        # source-aware audit cannot silently skip binding language there.
        start = 0 if index == 0 else match.start()
        page_segments.append((int(match.group(1)), text[start:end]))

    grouped: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_len = 0
    for page, segment in page_segments:
        if current and current_len + len(segment) > target_chars:
            grouped.append(current)
            current = []
            current_len = 0
        current.append((page, segment))
        current_len += len(segment)
    if current:
        grouped.append(current)

    units: list[SourceUnit] = []
    for group in grouped:
        unit_text = "".join(segment for _, segment in group)
        if not _meaningful_source(unit_text):
            continue
        pages = tuple(page for page, _ in group)
        label = f"pages {pages[0]}-{pages[-1]}" if len(pages) > 1 else f"page {pages[0]}"
        units.append(SourceUnit(index=len(units), text=unit_text, pages=pages, label=label))
    return units


def _blank_source_pages(text: str) -> list[int]:
    """Return marked pages that contain no extractable alphanumeric content."""

    matches = list(_PAGE_MARKER_RE.finditer(text or ""))
    blank: list[int] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        start = 0 if index == 0 else match.end()
        segment = text[start:end]
        if not _meaningful_source(segment):
            blank.append(int(match.group(1)))
    return blank


def _split_text_approximately(text: str, target_chars: int) -> list[str]:
    if len(text) <= target_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        desired = min(start + target_chars, len(text))
        if desired < len(text):
            paragraph = text.rfind("\n\n", start + target_chars // 2, desired)
            newline = text.rfind("\n", start + target_chars // 2, desired)
            cut = max(paragraph, newline)
            if cut <= start:
                cut = desired
        else:
            cut = desired
        chunks.append(text[start:cut])
        start = cut
    return [chunk for chunk in chunks if chunk]


def _split_unit(unit: SourceUnit) -> list[SourceUnit]:
    matches = list(_PAGE_MARKER_RE.finditer(unit.text))
    if len(matches) > 1:
        midpoint = len(matches) // 2
        cut = matches[midpoint].start()
        pieces = [unit.text[:cut], unit.text[cut:]]
    else:
        midpoint = len(unit.text) // 2
        paragraph = unit.text.rfind("\n\n", max(0, midpoint - 2000), midpoint + 2000)
        newline = unit.text.rfind("\n", max(0, midpoint - 2000), midpoint + 2000)
        cut = max(paragraph, newline)
        if cut <= 0 or cut >= len(unit.text):
            cut = midpoint
        if cut <= 0 or cut >= len(unit.text):
            return []
        pieces = [unit.text[:cut], unit.text[cut:]]

    children: list[SourceUnit] = []
    for index, piece in enumerate(pieces[:2]):
        child_pages = tuple(int(value) for value in _PAGE_MARKER_RE.findall(piece))
        if not child_pages:
            child_pages = unit.pages
        children.append(
            SourceUnit(
                index=unit.index,
                text=piece,
                pages=child_pages,
                label=f"{unit.label} part {index + 1}/2",
                depth=unit.depth + 1,
            )
        )
    return children


def _format_items(items: list[dict], unit: SourceUnit) -> str:
    pages = set(unit.pages)
    relevant = [
        item
        for item in items
        if item.get("source_page") in pages
        or (
            item.get("source_page") is None
            and _normalize_text(str(item.get("requirement_text") or ""))
            in _normalize_text(unit.text)
        )
    ]
    # For ordinary matrices, show the whole document index so a bad/missing
    # source_page does not create false omissions. Very large matrices use the
    # page-attributed subset to keep the source call bounded.
    displayed = items if len(items) <= 200 else relevant
    if not displayed:
        return "(none extracted for this document)"
    lines: list[str] = []
    for item in displayed:
        text = " ".join(str(item.get("requirement_text") or "").split())
        if len(text) > 1000:
            text = text[:1000].rstrip() + " [display capped]"
        lines.append(
            f"{item.get('requirement_id', '?')} | page {item.get('source_page') or '?'} "
            f"| {item.get('requirement_type', '')} | {item.get('category', '')} | {text}"
        )
    return "\n".join(lines)


def _usage_numbers(usage: dict | None) -> tuple[int, int, float]:
    usage = usage or {}
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        float(usage.get("cost_usd") or 0.0),
    )


def _is_represented(candidate: str, existing_items: list[dict]) -> bool:
    normalized = _normalize_text(candidate)
    if not normalized:
        return True
    for item in existing_items:
        current = _normalize_text(str(item.get("requirement_text") or ""))
        if not current:
            continue
        if normalized == current:
            return True
        # An existing row that fully contains the candidate represents it.
        # The inverse is deliberately unsafe: a truncated existing prefix must
        # not suppress a fuller source requirement from the omission audit.
        if min(len(normalized), len(current)) >= 40 and normalized in current:
            return True
    return False


def _near_duplicate(candidate: str, existing_items: list[dict]) -> bool:
    normalized = _normalize_text(candidate)
    for item in existing_items:
        current = _normalize_text(str(item.get("requirement_text") or ""))
        if not current:
            continue
        if SequenceMatcher(None, normalized, current).ratio() >= 0.82:
            return True
    return False


def _parse_payload_strict(
    payload: dict,
    unit: SourceUnit,
    existing_items: list[dict],
) -> tuple[list[MissingRequirementCandidate], list[UncertainPassage], int]:
    if "missing_candidates" not in payload or "uncertain_passages" not in payload:
        raise CompletenessProtocolError("completeness payload is missing required arrays")
    raw_candidates = payload["missing_candidates"]
    raw_uncertain = payload["uncertain_passages"]
    if not isinstance(raw_candidates, list) or not isinstance(raw_uncertain, list):
        raise CompletenessProtocolError("completeness payload fields must be arrays")

    candidates: list[MissingRequirementCandidate] = []
    duplicate_count = 0
    for index, row in enumerate(raw_candidates):
        if not isinstance(row, dict):
            raise CompletenessProtocolError(f"candidate {index} is not an object")
        quote = str(row.get("requirement_text") or "").strip()
        page = row.get("source_page")
        requirement_type = str(row.get("requirement_type") or "")
        category = str(row.get("category") or "")
        confidence = str(row.get("confidence") or "").upper()
        reason = str(row.get("reason") or "").strip()
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page not in set(unit.pages)
        ):
            raise CompletenessProtocolError(
                f"candidate {index} source_page {page!r} is outside {unit.label}"
            )
        page_source = _source_text_for_page(unit, page)
        if not quote or _normalize_text(quote) not in _normalize_text(page_source):
            raise CompletenessProtocolError(
                f"candidate {index} quote is not verbatim on source_page {page}"
            )
        if requirement_type not in _VALID_TYPES:
            raise CompletenessProtocolError(
                f"candidate {index} has invalid requirement_type {requirement_type!r}"
            )
        if category not in _VALID_CATEGORIES:
            raise CompletenessProtocolError(
                f"candidate {index} has invalid category {category!r}"
            )
        if confidence not in _VALID_CONFIDENCE or not reason:
            raise CompletenessProtocolError(
                f"candidate {index} has invalid confidence or empty reason"
            )
        weight = row.get("weight")
        if weight is not None and (
            isinstance(weight, bool) or not isinstance(weight, (int, float))
        ):
            raise CompletenessProtocolError(f"candidate {index} weight is not numeric")

        metadata_issues: list[str] = []
        if _PAGE_MARKER_RE.search(quote):
            metadata_issues.append("quotation includes an extraction page marker")
        quote_lines = [line.strip() for line in quote.splitlines() if line.strip()]
        if len(quote_lines) > 1:
            metadata_issues.append("quotation spans multiple extracted lines")
        if not _quote_is_source_boundary_aligned(quote, page_source):
            metadata_issues.append(
                "quotation does not align to a complete source sentence or line"
            )
        source_section: str | None = None
        if row.get("source_section") is not None:
            supplied_section = str(row.get("source_section") or "").strip()
            if supplied_section and _source_section_is_supported(
                supplied_section, page_source,
            ):
                source_section = supplied_section
            else:
                metadata_issues.append("source section is not supported by the source")

        verified_weight: float | None = None
        if weight is not None:
            supplied_weight = float(weight)
            if _weight_is_supported(
                supplied_weight,
                quote,
                requirement_type,
            ):
                verified_weight = supplied_weight
            else:
                metadata_issues.append("weight is not supported by the quoted requirement")

        complete_requirement = _looks_like_complete_requirement(
            quote, requirement_type,
        )
        if not complete_requirement:
            metadata_issues.append("quotation is not a complete requirement or criterion")
        type_supported = _requirement_type_is_supported(quote, requirement_type)
        if not type_supported:
            metadata_issues.append(
                "requirement type is not supported by the quoted language"
            )

        if metadata_issues:
            reason = (
                f"{reason.rstrip()} Deterministic verification requires manual review: "
                f"{'; '.join(metadata_issues)}."
            )

        if _is_represented(quote, existing_items):
            duplicate_count += 1
            continue
        near_duplicate = _near_duplicate(quote, existing_items)
        candidates.append(
            MissingRequirementCandidate(
                requirement_text=quote,
                source_page=page,
                source_section=source_section,
                requirement_type=requirement_type,
                category=category,
                weight=verified_weight,
                confidence=confidence,
                reason=reason,
                auto_add_eligible=(
                    confidence == "HIGH"
                    and not near_duplicate
                    and complete_requirement
                    and type_supported
                    and not metadata_issues
                ),
                near_duplicate=near_duplicate,
            )
        )

    uncertain: list[UncertainPassage] = []
    for index, row in enumerate(raw_uncertain):
        if not isinstance(row, dict):
            raise CompletenessProtocolError(f"uncertain passage {index} is not an object")
        page = row.get("source_page")
        reason = str(row.get("reason") or "").strip()
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page not in set(unit.pages)
            or not reason
        ):
            raise CompletenessProtocolError(
                f"uncertain passage {index} has invalid page or reason"
            )
        uncertain.append(UncertainPassage(source_page=page, reason=reason))
    return candidates, uncertain, duplicate_count


def _is_provider_wide_failure(exc: Exception) -> bool:
    # A malformed structured response is a batch/source-unit protocol failure,
    # not evidence that the provider itself is unavailable.  Keep this guard
    # ahead of both classifiers because protocol diagnostics can contain words
    # such as "timeout", "forbidden", or "credential" from model output.
    if isinstance(exc, CompletenessProtocolError):
        return False
    message = str(exc).lower()
    return is_transient_provider_error(exc) or any(
        marker in message
        for marker in (
            "api key",
            "credential",
            "unauthorized",
            "permission denied",
            "forbidden",
            "model not found",
            "status code: 401",
            "status code: 403",
            "status code: 404",
            "quota exceeded",
            "rate limit",
            "status code: 429",
            "status code: 500",
            "status code: 502",
            "status code: 503",
            "status code: 504",
            "service unavailable",
            "connection error",
            "connection reset",
            "connection refused",
            "timed out",
            "timeout",
        )
    )


def audit_compliance_completeness(
    *,
    source_text: str,
    source_filename: str,
    items: list[dict],
    proposal_id: int | None = None,
) -> ComplianceCompletenessReport:
    """Audit source coverage and return deterministically verified omissions."""

    settings = get_settings()
    primary_model = settings.model_compliance_validator
    fallback_model = settings.model_compliance_validator_fallback
    units = _source_units(source_text)
    matrix_fingerprint = "\n".join(
        f"{item.get('requirement_id')}|{item.get('requirement_text')}|"
        f"{item.get('source_page')}"
        for item in items
    )
    report = ComplianceCompletenessReport(
        source_units_total=len(units),
        primary_model=primary_model,
        fallback_model=fallback_model,
        source_sha256=hashlib.sha256((source_text or "").encode("utf-8")).hexdigest(),
        matrix_sha256=hashlib.sha256(matrix_fingerprint.encode("utf-8")).hexdigest(),
    )
    if not units:
        return report

    candidates_by_text: dict[str, MissingRequirementCandidate] = {}
    uncertain_by_key: dict[tuple[int, str], UncertainPassage] = {}
    for page in _blank_source_pages(source_text):
        passage = UncertainPassage(
            source_page=page,
            reason=(
                "No extractable text was available on this page; verify whether OCR "
                "or a text-searchable source is needed."
            ),
        )
        uncertain_by_key[(passage.source_page, passage.reason)] = passage

    def _call(
        unit: SourceUnit,
        *,
        model: str,
        role: Literal["primary", "fallback"],
    ) -> tuple[list[MissingRequirementCandidate], list[UncertainPassage], int, dict]:
        prompt = _USER_TEMPLATE.format(
            filename=source_filename,
            unit_label=unit.label,
            items_text=_format_items(items, unit),
            source_text=unit.text,
        )
        if role == "fallback":
            agent_name = "compliance_completeness_fallback"
        elif unit.depth:
            agent_name = "compliance_completeness_retry"
        else:
            agent_name = "compliance_completeness"
        payload, usage = call_tool_for_model(
            model=model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tool=_TOOL,
            max_tokens=6000,
            agent_name=agent_name,
            proposal_id=proposal_id,
        )
        parsed, uncertain, ignored = _parse_payload_strict(payload, unit, items)
        return parsed, uncertain, ignored, usage

    def _record(
        unit: SourceUnit,
        *,
        model: str,
        role: Literal["primary", "fallback"],
        success: bool,
        usage: dict | None = None,
        exc: Exception | None = None,
    ) -> None:
        in_tok, out_tok, cost = _usage_numbers(usage)
        report.attempts.append(
            CompletenessAttempt(
                model=model,
                role=role,
                unit_label=unit.label,
                depth=unit.depth,
                success=success,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                error_kind=type(exc).__name__ if exc is not None else None,
            )
        )

    def _accept(
        unit: SourceUnit,
        candidates: list[MissingRequirementCandidate],
        uncertain: list[UncertainPassage],
        ignored: int,
        *,
        role: Literal["primary", "fallback"],
    ) -> None:
        report.reviewed_unit_labels.append(unit.label)
        report.duplicate_candidates_ignored += ignored
        for candidate in candidates:
            candidate = replace(
                candidate,
                review_role=role,
                auto_add_eligible=(
                    candidate.auto_add_eligible and role == "primary"
                ),
            )
            key = _normalize_text(candidate.requirement_text)
            if key not in candidates_by_text or role == "primary":
                candidates_by_text[key] = candidate
        for passage in uncertain:
            uncertain_by_key[(passage.source_page, passage.reason)] = passage

    def _fallback(unit: SourceUnit) -> None:
        try:
            candidates, uncertain, ignored, usage = _call(
                unit,
                model=fallback_model,
                role="fallback",
            )
        except Exception as exc:
            _record(
                unit,
                model=fallback_model,
                role="fallback",
                success=False,
                exc=exc,
            )
            report.unresolved_unit_labels.append(unit.label)
            log.exception(
                "compliance_completeness: fallback failed for %s (%s)",
                source_filename,
                unit.label,
            )
            return
        _record(
            unit,
            model=fallback_model,
            role="fallback",
            success=True,
            usage=usage,
        )
        _accept(unit, candidates, uncertain, ignored, role="fallback")
        log.warning(
            "compliance_completeness: fallback reviewed %s %s -> %d candidate(s), %s",
            source_filename,
            unit.label,
            len(candidates),
            fmt_llm_usage(usage),
        )

    def _review_primary(unit: SourceUnit) -> None:
        try:
            candidates, uncertain, ignored, usage = _call(
                unit,
                model=primary_model,
                role="primary",
            )
        except Exception as exc:
            _record(
                unit,
                model=primary_model,
                role="primary",
                success=False,
                exc=exc,
            )
            children = (
                _split_unit(unit)
                if len(unit.text) > _MIN_RETRY_CHARS
                and unit.depth < _MAX_SPLIT_DEPTH
                and not _is_provider_wide_failure(exc)
                else []
            )
            if len(children) == 2:
                log.warning(
                    "compliance_completeness: primary failed for %s %s; "
                    "retrying two smaller source units",
                    source_filename,
                    unit.label,
                )
                for child in children:
                    _review_primary(child)
            else:
                _fallback(unit)
            return

        _record(
            unit,
            model=primary_model,
            role="primary",
            success=True,
            usage=usage,
        )
        _accept(unit, candidates, uncertain, ignored, role="primary")
        log.info(
            "compliance_completeness: primary reviewed %s %s -> %d candidate(s), %s",
            source_filename,
            unit.label,
            len(candidates),
            fmt_llm_usage(usage),
        )

    for unit in units:
        _review_primary(unit)

    report.candidates = list(candidates_by_text.values())
    report.uncertain_passages = list(uncertain_by_key.values())
    report.unresolved_unit_labels = list(dict.fromkeys(report.unresolved_unit_labels))
    # Failed parent calls are attempts, not terminal coverage units. When a
    # source unit is split, its successful/unresolved leaves become the true
    # denominator so reviewed_units can never exceed source_units_total.
    report.source_units_total = len(
        dict.fromkeys(
            [*report.reviewed_unit_labels, *report.unresolved_unit_labels]
        )
    )
    return report


__all__ = [
    "CompletenessProtocolError",
    "ComplianceCompletenessReport",
    "MissingRequirementCandidate",
    "UncertainPassage",
    "audit_compliance_completeness",
]
