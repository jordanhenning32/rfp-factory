"""Compliance Matrix Agent — extract every requirement / submission instruction
/ evaluation criterion from an RFP document.

Per design doc §6.2:
- VERBATIM extraction (no paraphrasing)
- Every item carries a source citation
- Sonnet-class model, tool-use for guaranteed structured output

For large RFPs (>100K chars), the document is split on page-marker
boundaries into chunks of ~60K chars each, run through the agent
in parallel, then merged + deduplicated + re-numbered. Sonnet on a
single 170K-char PDF takes ~5min; 3 chunks in parallel finish in
~2min — biggest single intake-time saving for typical state-IT
RFPs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import json_repair

from app.config import DATA_DIR, get_settings
from app.core.enums import RequirementCategory, RequirementType
from app.services.llm import fmt_llm_usage, get_anthropic

log = logging.getLogger(__name__)


_TOOL_SPEC: dict = {
    "name": "report_compliance_items",
    "description": (
        "Report every compliance requirement, submission instruction, and "
        "evaluation criterion extracted from the RFP document. Output ALL "
        "items found — do not summarize or skip."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "All extracted requirements. Empty array if none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_id": {
                            "type": "string",
                            "description": (
                                "Unique short ID like REQ-001, REQ-002, ... — sequential "
                                "starting from REQ-001."
                            ),
                        },
                        "requirement_text": {
                            "type": "string",
                            "description": (
                                "VERBATIM quote from the RFP document. Do not paraphrase, "
                                "summarize, or correct typos. Quote exactly as written. "
                                "Never truncate with '...' or end mid-word/mid-sentence — "
                                "include the FULL sentence(s) even if the requirement is "
                                "long. Only use '...' if the source RFP text itself "
                                "contains a literal ellipsis (e.g. inside a parenthetical "
                                "list like '(parents, students,...)')."
                            ),
                        },
                        "source_section": {
                            "type": ["string", "null"],
                            "description": (
                                "Section heading or number where the requirement appears, "
                                "e.g. 'Section 3.2 Technical Approach' or 'IV.B'."
                            ),
                        },
                        "source_page": {
                            "type": ["integer", "null"],
                            "description": (
                                "Page number (1-indexed) read from '--- Page N ---' markers in the input."
                            ),
                        },
                        "requirement_type": {
                            "type": "string",
                            "enum": [t.value for t in RequirementType],
                            "description": (
                                "shall/must = hard requirements; should = preferences; "
                                "submission_format = page limits/font/due date; "
                                "evaluation_criterion = scoring criteria; "
                                "mandatory_form = required forms or attachments."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": [c.value for c in RequirementCategory],
                        },
                        "weight": {
                            "type": ["number", "null"],
                            "description": (
                                "If this is an evaluation criterion with a stated point "
                                "value or percentage weight, the number. Otherwise null."
                            ),
                        },
                    },
                    "required": [
                        "requirement_id",
                        "requirement_text",
                        "requirement_type",
                        "category",
                    ],
                },
            }
        },
        "required": ["items"],
    },
}


_SYSTEM = """You are a compliance-extraction expert for U.S. government RFP responses.

Your job: read an RFP document and identify every requirement, submission instruction, and evaluation criterion that a proposer must address.

Hard rules:
1. VERBATIM extraction. Quote requirement language EXACTLY as it appears. Do not paraphrase, summarize, or correct grammar.
2. NO TRUNCATION. Include the COMPLETE sentence(s) for each requirement. Never end requirement_text mid-word ("Vendo", "publishin", "begi") or mid-sentence ("...notify the"). Never use "..." to abbreviate — it's misrepresentation in a procurement context. Long requirements stay long; if the source spans 200 words, output 200 words. Only reproduce literal "..." that appear in the source RFP itself (e.g., inside parentheticals like "(parents, students,...)").
3. Every item must include source_page (1-indexed). Read it from "--- Page N ---" markers in the input.
4. "Shall" / "must" / "is required to" → hard requirements (type: "shall" or "must").
5. "Should" / "preferred" / "encouraged" → preferences (type: "should").
6. Page limits, font specs, file format, deadlines, delivery method → "submission_format".
7. Sections that describe how proposals will be scored → "evaluation_criterion" (capture weight if a number is stated).
8. Required forms, attachments, certifications-to-attach → "mandatory_form".
9. Boilerplate background / agency history that contains no requirement → SKIP. Do not invent items.
10. Section headings, subheadings, list-item labels (e.g. "a. ", "1. ", "Section 3.2 Technical Approach"), and form-field labels (e.g. "YES NO") are NOT requirements by themselves — SKIP them unless the heading text itself states a requirement.

Use the report_compliance_items tool to return ALL items in one call. Do not include any commentary.

AMENDMENT DELTA MODE — You are processing an amendment or Q&A response, not the original RFP. The PRE-EXISTING REQUIREMENTS block lists every active requirement from prior documents. Your output is the DELTA only:
 - new_items: requirements the amendment introduces that have no equivalent in the pre-existing list. NEVER emit a requirement that paraphrases an existing one — instead emit it as a modified_item with the existing_id.
 - modified_items: pre-existing requirements whose text the amendment changes. Cite the existing_id verbatim from the PRE-EXISTING block. Include a one-sentence change_summary (e.g., 'page limit raised from 25 to 30').
 - removed_items: pre-existing requirements the amendment explicitly cancels. Cite the existing_id. Include the reason verbatim from the amendment.
Never invent existing_ids. Never emit duplicates. If unsure whether a paragraph is new or a clarification of an existing requirement, prefer modified_items + a clear change_summary."""


_USER = """Extract every compliance requirement from this RFP document.

DOCUMENT FILENAME: {filename}

DOCUMENT TEXT:
{text}

Call the report_compliance_items tool with all extracted items."""


_USER_DELTA = """Extract the DELTA between this amendment/Q&A document and the pre-existing compliance matrix.

DOCUMENT FILENAME: {filename}

PRE-EXISTING REQUIREMENTS (for reference; do NOT re-emit):
{existing_items_text}

DOCUMENT TEXT:
{text}

Call the report_compliance_delta tool with new_items + modified_items + removed_items arrays."""


_TOOL_SPEC_DELTA: dict = {
    "name": "report_compliance_delta",
    "description": (
        "Report the DELTA between this amendment/Q&A document and the "
        "pre-existing compliance matrix. Output ALL changes — new requirements, "
        "modifications, and removals. Empty arrays are fine when a section "
        "introduces no changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "new_items": {
                "type": "array",
                "description": (
                    "Requirements the amendment introduces. NEVER include a "
                    "rewording of a pre-existing requirement here — that "
                    "belongs in modified_items."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_id": {
                            "type": "string",
                            "description": (
                                "Placeholder — the apply layer reassigns "
                                "REQ-NNN sequentially; the value you choose "
                                "here is discarded."
                            ),
                        },
                        "requirement_text": {
                            "type": "string",
                            "description": (
                                "VERBATIM quote from the amendment text. "
                                "Same rules as the full extraction tool: "
                                "no paraphrasing, no truncation."
                            ),
                        },
                        "source_section": {
                            "type": ["string", "null"],
                        },
                        "source_page": {
                            "type": ["integer", "null"],
                        },
                        "requirement_type": {
                            "type": "string",
                            "enum": [t.value for t in RequirementType],
                        },
                        "category": {
                            "type": "string",
                            "enum": [c.value for c in RequirementCategory],
                        },
                        "weight": {
                            "type": ["number", "null"],
                        },
                    },
                    "required": [
                        "requirement_id",
                        "requirement_text",
                        "requirement_type",
                        "category",
                    ],
                },
            },
            "modified_items": {
                "type": "array",
                "description": (
                    "Pre-existing requirements whose text the amendment "
                    "changes. existing_id must match a requirement_id in "
                    "the PRE-EXISTING REQUIREMENTS block exactly."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "existing_id": {
                            "type": "string",
                            "description": (
                                "REQ-NNN id from the PRE-EXISTING REQUIREMENTS block. Must match verbatim."
                            ),
                        },
                        "new_text": {
                            "type": "string",
                            "description": (
                                "VERBATIM new requirement text from the amendment. No paraphrasing."
                            ),
                        },
                        "change_summary": {
                            "type": "string",
                            "description": (
                                "One sentence describing the change "
                                "(e.g., 'page limit raised from 25 to 30')."
                            ),
                        },
                    },
                    "required": ["existing_id", "new_text", "change_summary"],
                },
            },
            "removed_items": {
                "type": "array",
                "description": ("Pre-existing requirements the amendment explicitly cancels."),
                "items": {
                    "type": "object",
                    "properties": {
                        "existing_id": {
                            "type": "string",
                            "description": ("REQ-NNN id from the PRE-EXISTING REQUIREMENTS block."),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "Verbatim reason from the amendment, or "
                                "a one-sentence summary if the amendment "
                                "doesn't give an explicit reason."
                            ),
                        },
                    },
                    "required": ["existing_id", "reason"],
                },
            },
        },
        "required": ["new_items", "modified_items", "removed_items"],
    },
}


@dataclass
class ExtractedComplianceItem:
    requirement_id: str
    requirement_text: str
    requirement_type: str
    category: str
    source_section: str | None = None
    source_page: int | None = None
    weight: float | None = None
    # Runtime provenance used by intake when a verified omission is recovered
    # by the independent source-completeness audit. Primary extraction and
    # amendment callers keep the default.
    extraction_origin: str = "primary"


_VALID_REQUIREMENT_TYPES = {value.value for value in RequirementType}
_VALID_REQUIREMENT_CATEGORIES = {value.value for value in RequirementCategory}


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _weight_is_grounded(
    weight: float,
    requirement_text: str,
    requirement_type: str,
) -> bool:
    if requirement_type != RequirementType.EVALUATION_CRITERION.value:
        return False
    rendered = format(weight, ".15g")
    if "." in rendered:
        whole, fraction = rendered.split(".", 1)
        rendered = rf"{re.escape(whole)}\.{re.escape(fraction)}0*"
    else:
        rendered = rf"{re.escape(rendered)}(?:\.0+)?"
    number = rf"(?<![\d.]){rendered}(?![\d.])"
    if re.search(
        rf"{number}\s*(?:%|percent\b|points?\b)",
        requirement_text,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"\b(?:weight(?:ed)?|worth|score(?:d)?)\b\D{{0,24}}{number}",
        requirement_text,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"{number}\D{{0,24}}\b(?:weight|weighted|points?|percent)\b",
        requirement_text,
        re.IGNORECASE,
    ):
        return True
    if 0 < weight <= 1:
        percent_value = weight * 100
        percent = format(percent_value, ".15g")
        if "." in percent:
            whole, fraction = percent.split(".", 1)
            percent = rf"{re.escape(whole)}\.{re.escape(fraction)}0*"
        else:
            percent = rf"{re.escape(percent)}(?:\.0+)?"
        return re.search(
            rf"(?<![\d.]){percent}(?![\d.])\s*(?:%|percent\b)",
            requirement_text,
            re.IGNORECASE,
        ) is not None
    return False


def _normalized_source_body(text: str) -> str:
    """Normalize source prose while removing parser page-marker metadata."""

    return _normalize_for_dedup(_PAGE_MARKER_RE.sub(" ", text or ""))


def _requirement_is_grounded(
    requirement_text: str,
    *,
    source_pages: dict[int, str],
    source_page: int | None,
) -> bool:
    """Verify quoted text in the source, including adjacent-page sentences.

    PDF text extraction commonly inserts a page marker in the middle of one
    sentence.  For a cited page, allow a quote to span its immediate neighbor
    pages, but require the actual matched interval to overlap the cited page.
    This keeps page citations meaningful while avoiding false rejections at a
    physical page break.
    """

    candidate = _normalize_for_dedup(requirement_text)
    if not candidate:
        return False
    page_numbers = list(source_pages)
    normalized_pages = {
        page: _normalized_source_body(source_pages[page]) for page in page_numbers
    }
    if source_page is None:
        return candidate in " ".join(normalized_pages.values())
    if source_page not in normalized_pages:
        return False

    cited_index = page_numbers.index(source_page)
    first = max(0, cited_index - 1)
    last = min(len(page_numbers), cited_index + 2)
    window_pages = page_numbers[first:last]
    pieces: list[str] = []
    cited_start = cited_end = 0
    cursor = 0
    for page in window_pages:
        if pieces:
            cursor += 1  # space inserted by the join below
        body = normalized_pages[page]
        start = cursor
        cursor += len(body)
        if page == source_page:
            cited_start, cited_end = start, cursor
        pieces.append(body)
    window = " ".join(pieces)
    start = window.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if start < cited_end and end > cited_start:
            return True
        start = window.find(candidate, start + 1)
    return False


def _source_section_is_grounded(
    source_section: str,
    *,
    source_pages: dict[int, str],
    source_page: int | None,
) -> bool:
    """Retain optional section metadata only when a structural line supports it."""

    page_numbers = list(source_pages)
    if source_page is None:
        relevant_pages = page_numbers
    else:
        cited_index = page_numbers.index(source_page)
        relevant_pages = page_numbers[
            max(0, cited_index - 1) : min(len(page_numbers), cited_index + 2)
        ]
    source = "\n".join(source_pages[p] for p in relevant_pages)
    needle = _normalize_for_dedup(source_section)
    if not needle:
        return False
    identifier_match = re.fullmatch(
        r"(?:section\s+)?(?:[a-z](?:\.\d+)*|\d+(?:\.\d+)*)",
        needle,
        re.IGNORECASE,
    )
    named = len(re.findall(r"[a-z0-9]+", needle, re.IGNORECASE)) >= 2
    if identifier_match is None and not named:
        return False

    bare_identifier = re.sub(r"^section\s+", "", needle, flags=re.IGNORECASE)
    identifier_patterns = (bare_identifier, f"section {bare_identifier}")

    def _requirement_like(line: str) -> bool:
        return bool(
            re.search(r"\b(?:shall|must|should)\b", line, re.IGNORECASE)
            or re.search(r"\b(?:is|are)\s+required\s+to\b", line, re.IGNORECASE)
            or re.search(
                r"\b(?:offeror|proposer|respondent|bidder|vendor|contractor|"
                r"supplier|applicant)s?\b.{0,100}\bwill\b",
                line,
                re.IGNORECASE,
            )
            or re.search(
                r"^(?:please\s+)?(?:submit|provide|attach|include|complete|sign|"
                r"upload|describe|explain|identify|list|demonstrate|certify|"
                r"acknowledge|return|furnish|respond|state|indicate|document|"
                r"price|address)\b",
                line,
                re.IGNORECASE,
            )
            or re.search(
                r"\b(?:is|are)\s+due\b|\bmay\s+not\s+exceed\b|"
                r"\bno\s+more\s+than\b|\blimited\s+to\b",
                line,
                re.IGNORECASE,
            )
            or (
                re.search(
                    r"\b(?:evaluat(?:e|ed|es|ion)|scor(?:e|ed|es|ing)|points?|"
                    r"weight(?:ed|ing)?|award\s+factor|rating|preference)\b",
                    line,
                    re.IGNORECASE,
                )
                and re.search(r"\b(?:will|is|are|shall|must)\b", line, re.IGNORECASE)
            )
        )

    for raw_line in _PAGE_MARKER_RE.sub("", source).splitlines():
        line = _normalize_for_dedup(raw_line)
        if not line or len(line) > 180:
            continue
        if identifier_match is not None and any(
            line == pattern
            or re.match(rf"^{re.escape(pattern)}(?:\s|[:.\-])", line)
            for pattern in identifier_patterns
        ):
            return True
        if (
            named
            and not _requirement_like(line)
            and (line == needle or line.startswith(needle + " "))
        ):
            return True
    return False


def _strict_extracted_item(
    raw: object,
    *,
    source_pages: dict[int, str],
) -> ExtractedComplianceItem:
    """Validate one tool item without coercing malformed values to strings."""

    if not isinstance(raw, dict):
        raise ValueError("item must be an object")
    requirement_id = _nonempty_string(raw.get("requirement_id"), "requirement_id")
    requirement_text = _nonempty_string(
        raw.get("requirement_text"),
        "requirement_text",
    )
    requirement_type = _nonempty_string(
        raw.get("requirement_type"),
        "requirement_type",
    )
    if requirement_type not in _VALID_REQUIREMENT_TYPES:
        raise ValueError(f"invalid requirement_type {requirement_type!r}")
    category = _nonempty_string(raw.get("category"), "category")
    if category not in _VALID_REQUIREMENT_CATEGORIES:
        raise ValueError(f"invalid category {category!r}")

    source_section = raw.get("source_section")
    if source_section is not None:
        source_section = _nonempty_string(source_section, "source_section")
    source_page = raw.get("source_page")
    if source_page is not None and (
        isinstance(source_page, bool)
        or not isinstance(source_page, int)
        or source_page < 1
    ):
        raise ValueError("source_page must be a positive integer or null")
    if source_page is not None and source_page not in source_pages:
        raise ValueError(f"source_page {source_page} is not present in the source")

    if not _requirement_is_grounded(
        requirement_text,
        source_pages=source_pages,
        source_page=source_page,
    ):
        raise ValueError("requirement_text is not grounded on the cited source page")
    if source_section is not None and not _source_section_is_grounded(
        source_section,
        source_pages=source_pages,
        source_page=source_page,
    ):
        # The section is optional metadata. Discard an unsupported label rather
        # than presenting model-invented provenance or rejecting an otherwise
        # source-grounded requirement.
        source_section = None

    weight = raw.get("weight")
    if weight is not None:
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or float(weight) < 0
        ):
            raise ValueError("weight must be a finite non-negative number or null")
        weight = float(weight)
        if not _weight_is_grounded(weight, requirement_text, requirement_type):
            raise ValueError(
                "weight is not grounded by evaluation language in requirement_text"
            )

    return ExtractedComplianceItem(
        requirement_id=requirement_id,
        requirement_text=requirement_text,
        requirement_type=requirement_type,
        category=category,
        source_section=source_section,
        source_page=source_page,
        weight=weight,
    )


@dataclass
class ComplianceExtractionResult:
    """Return type for extract_compliance_items.

    Legacy callers (delta_mode=False) read `items` and ignore the delta
    fields (which remain empty). Delta-mode callers (delta_mode=True) read
    `new_items`, `modified_items`, `removed_items` and ignore `items`
    (which remains empty). Backwards-compatible: the dataclass replaces
    the bare list return — callers must now read `result.items`.

    - items: list[ExtractedComplianceItem] — full extraction for the
        legacy (non-delta) path.
    - new_items: list[ExtractedComplianceItem] — requirements the
        amendment introduces.
    - modified_items: list[dict] with keys `existing_id`, `new_text`,
        `change_summary` — pre-existing requirements the amendment
        rewrites.
    - removed_items: list[dict] with keys `existing_id`, `reason` —
        pre-existing requirements the amendment cancels.
    - coverage_state plus the coverage indicator fields distinguish a clean
        result from partial/failed extraction. `coverage_as_public_dict()` is
        the stable JSON-safe representation intended for durable UI state.
    """

    items: list[ExtractedComplianceItem] = field(default_factory=list)
    new_items: list[ExtractedComplianceItem] = field(default_factory=list)
    modified_items: list[dict] = field(default_factory=list)
    removed_items: list[dict] = field(default_factory=list)
    # Coverage metadata is intentionally made of JSON-safe primitives so the
    # intake layer can persist it directly in a document's structure_json.
    # Defaults preserve compatibility for tests and callers that construct a
    # synthetic result themselves; every real extraction path below supplies
    # explicit values.
    coverage_state: str = "complete"
    source_chunks_total: int = 0
    source_chunks_completed: int = 0
    failed_chunk_labels: list[str] = field(default_factory=list)
    source_truncated: bool = False
    response_truncated: bool = False
    output_capped: bool = False
    malformed_items_skipped: int = 0
    incomplete_reasons: list[str] = field(default_factory=list)

    @property
    def extraction_complete(self) -> bool:
        """Whether every source unit was processed without data loss."""

        return self.coverage_state == "complete"

    @property
    def failed_chunk_count(self) -> int:
        return len(self.failed_chunk_labels)

    def coverage_as_public_dict(self) -> dict:
        """Return the durable extraction-coverage summary for the UI/state."""

        return {
            "state": self.coverage_state,
            "complete": self.extraction_complete,
            "source_chunks_total": self.source_chunks_total,
            "source_chunks_completed": self.source_chunks_completed,
            "failed_chunk_count": self.failed_chunk_count,
            "failed_chunk_labels": list(self.failed_chunk_labels),
            "source_truncated": self.source_truncated,
            "response_truncated": self.response_truncated,
            "output_capped": self.output_capped,
            "malformed_items_skipped": self.malformed_items_skipped,
            "incomplete_reasons": list(self.incomplete_reasons),
        }


@dataclass
class _ExtractionCoverage:
    """Mutable per-call accumulator used to build public coverage metadata."""

    source_chunks_total: int = 0
    source_chunks_completed: int = 0
    failed_chunk_labels: list[str] = field(default_factory=list)
    source_truncated: bool = False
    response_truncated: bool = False
    output_capped: bool = False
    malformed_items_skipped: int = 0
    incomplete_reasons: list[str] = field(default_factory=list)

    def flag(self, reason: str) -> None:
        if reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)

    def fail_chunk(self, label: str, reason: str) -> None:
        if label not in self.failed_chunk_labels:
            self.failed_chunk_labels.append(label)
        self.flag(reason)

    def merge(self, other: _ExtractionCoverage) -> None:
        self.source_chunks_total += other.source_chunks_total
        self.source_chunks_completed += other.source_chunks_completed
        for label in other.failed_chunk_labels:
            if label not in self.failed_chunk_labels:
                self.failed_chunk_labels.append(label)
        self.source_truncated = self.source_truncated or other.source_truncated
        self.response_truncated = (
            self.response_truncated or other.response_truncated
        )
        self.output_capped = self.output_capped or other.output_capped
        self.malformed_items_skipped += other.malformed_items_skipped
        for reason in other.incomplete_reasons:
            self.flag(reason)

    def state(self, *, returned_item_count: int) -> str:
        has_loss = bool(
            self.failed_chunk_labels
            or self.source_truncated
            or self.response_truncated
            or self.output_capped
            or self.malformed_items_skipped
            or self.incomplete_reasons
            or self.source_chunks_completed < self.source_chunks_total
        )
        if not has_loss:
            return "complete"
        if self.source_chunks_completed == 0 and returned_item_count == 0:
            return "failed"
        return "partial"

    def result_kwargs(self, *, returned_item_count: int) -> dict:
        return {
            "coverage_state": self.state(
                returned_item_count=returned_item_count,
            ),
            "source_chunks_total": self.source_chunks_total,
            "source_chunks_completed": self.source_chunks_completed,
            "failed_chunk_labels": list(self.failed_chunk_labels),
            "source_truncated": self.source_truncated,
            "response_truncated": self.response_truncated,
            "output_capped": self.output_capped,
            "malformed_items_skipped": self.malformed_items_skipped,
            "incomplete_reasons": list(self.incomplete_reasons),
        }


# Cap input chars to keep cost predictable. ~150K chars ≈ ~38K tokens, well
# inside Sonnet's 200K context. Most state RFPs fit comfortably.
_MAX_INPUT_CHARS = 200_000

# Chunking thresholds for big single-doc RFPs. When the document
# exceeds CHUNK_THRESHOLD_CHARS, split on `--- Page N ---` boundaries
# into pieces of approximately CHUNK_TARGET_CHARS each, run through
# the agent in parallel, and merge. Page-aligned chunks preserve the
# `source_page` extraction behavior because each chunk retains its
# own page markers.
_CHUNK_TARGET_CHARS = 60_000
_CHUNK_THRESHOLD_CHARS = 100_000

# Page marker the parser embeds: "--- Page 1 ---" on its own line.
# Used to split the doc on natural boundaries.
_PAGE_MARKER_RE = re.compile(
    r"^---\s*Page\s+(\d+)\s*---$",
    re.MULTILINE | re.IGNORECASE,
)


def _source_pages(text: str) -> dict[int, str]:
    """Return canonical page segments, preserving any leading preamble."""

    matches = list(_PAGE_MARKER_RE.finditer(text or ""))
    if not matches:
        return {1: text}
    pages: dict[int, str] = {}
    for index, match in enumerate(matches):
        start = 0 if index == 0 else match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        page = int(match.group(1))
        pages[page] = pages.get(page, "") + text[start:end]
    return pages


def _split_text_by_pages(
    text: str,
    target_chars: int = _CHUNK_TARGET_CHARS,
) -> list[str]:
    """Split RFP text into chunks of approximately `target_chars`,
    breaking ONLY at `--- Page N ---` markers so requirements that
    span page boundaries within the same chunk stay intact.

    Returns the original text as a single-element list when there
    are no page markers (defensive — our parser always emits them
    so this should not happen in practice).

    Greedy packing: walk pages in order, accumulate into the current
    chunk until adding the next page would exceed `target_chars`,
    then flush and start a new chunk. Last chunk may exceed
    `target_chars` if a single page is itself huge — better to send
    one oversized chunk than to split mid-page and lose context."""
    if len(text) <= target_chars:
        return [text]
    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        return [text]

    page_starts = [m.start() for m in matches]
    page_starts.append(len(text))  # sentinel: end of doc

    chunks: list[str] = []
    # Include any parser preamble before the first page marker. Starting at
    # page_starts[0] silently discarded it and could make a chunked extraction
    # look complete even though the model never saw the leading source text.
    chunk_start = 0
    for i in range(1, len(page_starts) - 1):
        # Position where page i+1 begins (= end of page i)
        next_break = page_starts[i]
        if (next_break - chunk_start) >= target_chars:
            # The current chunk has filled up to `target_chars` worth
            # of pages. Cut here.
            chunks.append(text[chunk_start:next_break])
            chunk_start = next_break
    # Tail: from the last cut to end-of-doc
    if chunk_start < len(text):
        chunks.append(text[chunk_start:])
    return chunks


def _normalize_for_dedup(text: str) -> str:
    """Whitespace-collapsed, lowered key for comparing requirement_text
    across chunks. Sonnet quotes verbatim, so duplicates (rare —
    only if a requirement somehow gets attributed to two pages and
    both pages are in different chunks) come out byte-equal modulo
    whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _parse_items_string(raw: str, *, filename: str) -> list | None:
    """Try to recover a list from a string that the SDK delivered in
    place of the structured `items` array. Returns the parsed list on
    success, None if neither strategy yields a list. Logs which path
    succeeded so we can monitor how often each kicks in.

    Strategy 1: plain `json.loads` — handles the case where the model
    cleanly JSON-encoded the entire array as a string.

    Strategy 2: `json_repair.loads` — handles the more common case
    where the JSON has unescaped chars inside string values
    (e.g. literal `"` from verbatim RFP quotes like ("Warranty
    Period")). json_repair is purpose-built for LLM-malformed JSON.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        log.warning(
            "compliance_matrix: %s 'items' was a JSON-encoded string — recovered via json.loads (%d items).",
            filename,
            len(parsed),
        )
        return parsed

    try:
        repaired = json_repair.loads(raw)
    except Exception:  # noqa: BLE001 — defend against any repair-time crash
        return None
    if isinstance(repaired, list):
        log.warning(
            "compliance_matrix: %s 'items' had malformed JSON (likely "
            "unescaped chars in verbatim quotes) — recovered via "
            "json_repair (%d items).",
            filename,
            len(repaired),
        )
        return repaired
    return None


def _dump_failed_payload(
    *,
    raw: str,
    filename: str,
    recursion_depth: int,
    parse_error: str,
) -> Path | None:
    """Write redacted failure metadata without retaining proprietary source text."""
    try:
        dump_dir = DATA_DIR / "debug" / "compliance_matrix"
        dump_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        safe_name = re.sub(r"[^\w-]", "_", filename)[:80]
        dump_path = dump_dir / f"{ts}_{safe_name}_d{recursion_depth}.txt"
        header = (
            f"# parse_error: {parse_error}\n"
            f"# filename: {filename}\n"
            f"# recursion_depth: {recursion_depth}\n"
            f"# raw_len: {len(raw)}\n"
            f"# raw_sha256: {hashlib.sha256(raw.encode('utf-8')).hexdigest()}\n"
            "# raw payload intentionally omitted\n"
        )
        dump_path.write_text(header, encoding="utf-8")
        return dump_path
    except Exception:  # noqa: BLE001 — best-effort debug aid
        log.exception("compliance_matrix: failed to write debug dump")
        return None


def _recover_via_split(
    *,
    document_text: str,
    filename: str,
    proposal_id: int,
    max_items: int,
    recursion_depth: int,
    reason: str,
    coverage: _ExtractionCoverage,
    coverage_label: str,
) -> list[ExtractedComplianceItem]:
    """When `_extract_one_chunk` got a malformed tool input back
    (un-parseable string, wrong type), halve the chunk on page
    boundaries and recurse. Returns [] if we've exhausted retries
    or the chunk can't be split further (no internal page marker
    splits at the requested target).

    Bounded at recursion_depth=1 — if a halved chunk *also* fails
    to produce valid JSON, the issue isn't size and recursing
    further wastes API spend without fixing anything.
    """
    if recursion_depth >= 1:
        log.error(
            "compliance_matrix: %s — recovery exhausted (depth=%d, reason=%s); chunk dropped permanently.",
            filename,
            recursion_depth,
            reason,
        )
        coverage.fail_chunk(
            coverage_label,
            "malformed_tool_payload_unrecovered",
        )
        return []

    target = max(10_000, len(document_text) // 2)
    halves = _split_text_by_pages(document_text, target_chars=target)
    if len(halves) < 2:
        log.error(
            "compliance_matrix: %s — cannot split further "
            "(reason=%s, len=%d, target=%d, got %d sub-chunk(s)); "
            "chunk dropped.",
            filename,
            reason,
            len(document_text),
            target,
            len(halves),
        )
        coverage.fail_chunk(
            coverage_label,
            "malformed_tool_payload_unrecovered",
        )
        return []

    log.warning(
        "compliance_matrix: %s — recovery (reason=%s): re-splitting "
        "%d-char chunk into %d sub-chunk(s) and retrying",
        filename,
        reason,
        len(document_text),
        len(halves),
    )

    results: list[ExtractedComplianceItem] = []
    for sub_idx, half in enumerate(halves):
        results.extend(
            _extract_one_chunk(
                document_text=half,
                filename=f"{filename} retry {sub_idx + 1}/{len(halves)}",
                proposal_id=proposal_id,
                max_items=max_items,
                recursion_depth=recursion_depth + 1,
                coverage=coverage,
                coverage_label=(
                    f"{coverage_label} retry {sub_idx + 1}/{len(halves)}"
                ),
            )
        )

    log.info(
        "compliance_matrix: %s — recovery yielded %d item(s) from %d sub-chunk(s)",
        filename,
        len(results),
        len(halves),
    )
    return results


def _extract_one_chunk(
    *,
    document_text: str,
    filename: str,
    proposal_id: int,
    max_items: int = 500,
    recursion_depth: int = 0,
    coverage: _ExtractionCoverage | None = None,
    coverage_label: str = "document",
) -> list[ExtractedComplianceItem]:
    """Single Sonnet call against one chunk (or the whole doc when
    it fits inside the threshold). Caller handles dispatch + merge.

    On malformed-JSON failures (model produced unescaped chars in a
    verbatim quote and the streaming parser gave us a raw string),
    halve the chunk on page boundaries and recurse. Bounded by
    `recursion_depth` to avoid runaway costs on a pathological chunk.
    """
    coverage = coverage or _ExtractionCoverage()
    settings = get_settings()
    client = get_anthropic()
    text = document_text[:_MAX_INPUT_CHARS]
    if len(document_text) > _MAX_INPUT_CHARS:
        log.warning(
            "compliance_matrix: %s truncated from %d to %d chars",
            filename,
            len(document_text),
            _MAX_INPUT_CHARS,
        )
        coverage.source_truncated = True
        coverage.flag("source_truncated")

    tool_input, usage = client.call_tool(
        model=settings.model_compliance_matrix,  # Sonnet 4.6 by default
        system=_SYSTEM,
        messages=[{"role": "user", "content": _USER.format(filename=filename, text=text)}],
        tool=_TOOL_SPEC,
        max_tokens=32000,
        agent_name="compliance_matrix",
        proposal_id=proposal_id,
    )

    needs_recovery = False
    recovery_reason = ""
    if not isinstance(tool_input, dict):
        raw_items = None
        needs_recovery = True
        recovery_reason = f"tool input is {type(tool_input).__name__}"
        log.error(
            "compliance_matrix: %s tool input is %s, not dict — "
            "attempting bounded recovery.",
            filename, type(tool_input).__name__,
        )
    elif "items" not in tool_input:
        raw_items = None
        needs_recovery = True
        recovery_reason = "required items field missing"
        log.error(
            "compliance_matrix: %s tool input omitted required 'items' "
            "field — attempting bounded recovery.",
            filename,
        )
    else:
        raw_items = tool_input["items"]

    # Anthropic streaming tool-use can return `items` as a raw JSON
    # *string* instead of a parsed list. Two seen modes:
    #   (a) the array was JSON-encoded as a string — `json.loads` recovers
    #   (b) the model emitted JSON with unescaped chars in string values
    #       (commonly literal `"` chars in verbatim quotes from the RFP,
    #       e.g. ("Warranty Period")) and the SDK's partial-JSON parser
    #       left the un-parseable subtree as a raw string — `json_repair`
    #       recovers since the lib was built for exactly this.
    # Only fall through to split-and-retry if BOTH parsers fail.
    if not needs_recovery and isinstance(raw_items, str):
        parsed = _parse_items_string(raw_items, filename=filename)
        if parsed is None:
            dump_path = _dump_failed_payload(
                raw=raw_items,
                filename=filename,
                recursion_depth=recursion_depth,
                parse_error="json.loads + json_repair both failed",
            )
            log.error(
                "compliance_matrix: %s 'items' un-parseable by both "
                "json.loads and json_repair (len=%d) — raw dump: %s",
                filename,
                len(raw_items),
                dump_path or "(dump failed)",
            )
            needs_recovery = True
            recovery_reason = f"un-repairable string (len={len(raw_items)})"
        else:
            raw_items = parsed
    elif not needs_recovery and not isinstance(raw_items, list):
        log.error(
            "compliance_matrix: %s 'items' is %s, not list — chunk dropped.",
            filename,
            type(raw_items).__name__,
        )
        needs_recovery = True
        recovery_reason = f"items is {type(raw_items).__name__}"

    if needs_recovery:
        return _recover_via_split(
            document_text=text,
            filename=filename,
            proposal_id=proposal_id,
            max_items=max_items,
            recursion_depth=recursion_depth,
            reason=recovery_reason,
            coverage=coverage,
            coverage_label=coverage_label,
        )

    log.info(
        "compliance_matrix: %s -> %d items, %s stop=%s",
        filename,
        len(raw_items),
        fmt_llm_usage(usage),
        usage.get("stop_reason"),
    )

    # If the model returned no items but the output was truncated, the tool
    # call probably got cut off before the JSON closed.
    if usage.get("stop_reason") == "max_tokens":
        coverage.response_truncated = True
        coverage.flag("response_truncated")
    if not raw_items and usage.get("stop_reason") == "max_tokens":
        log.error(
            "compliance_matrix: %s returned 0 items AND hit max_tokens — "
            "the response was truncated mid-JSON. Increase max_tokens further.",
            filename,
        )
        coverage.fail_chunk(coverage_label, "response_truncated_without_items")

    if len(raw_items) > max_items:
        log.warning(
            "compliance_matrix: capping items at %d (got %d)",
            max_items,
            len(raw_items),
        )
        coverage.output_capped = True
        coverage.flag("output_capped")
        raw_items = raw_items[:max_items]

    extracted: list[ExtractedComplianceItem] = []
    source_pages = _source_pages(text)
    for index, item in enumerate(raw_items):
        try:
            extracted.append(
                _strict_extracted_item(item, source_pages=source_pages)
            )
        except (TypeError, ValueError) as exc:
            coverage.malformed_items_skipped += 1
            coverage.flag("malformed_items_skipped")
            log.warning(
                "compliance_matrix: skipping malformed item %d: %s",
                index,
                exc,
            )
    return extracted


def _format_existing_items(existing_items: list[dict]) -> str:
    """Render the PRE-EXISTING REQUIREMENTS block for the delta user prompt.

    Returns '(none)' for an empty input so the prompt still parses cleanly
    when the proposal has no current items (unusual but possible — e.g.,
    if compliance extraction failed during intake and the user is uploading
    an amendment before re-running it).
    """
    if not existing_items:
        return "(none)"
    lines = [f"{r.get('requirement_id', '?')}: {r.get('requirement_text', '')}" for r in existing_items]
    return "\n".join(lines)


def _extract_delta_chunk(
    *,
    document_text: str,
    filename: str,
    proposal_id: int,
    existing_items: list[dict],
    max_items: int = 500,
) -> ComplianceExtractionResult:
    """Single Sonnet call against an amendment / Q&A document in delta mode.

    Returns a ComplianceExtractionResult populated with `new_items`,
    `modified_items`, `removed_items` (and `items=[]`). No chunking — delta
    inputs are typically small (single amendment PDF / Q&A doc). Oversize
    inputs (> _MAX_INPUT_CHARS) get truncated with a warning, matching the
    behavior of `_extract_one_chunk` on the legacy path.

    Each of the three list fields is run through the same string-recovery
    fallback (`_parse_items_string`) the legacy path uses, so the
    Anthropic streaming tool-use string-instead-of-array failure mode is
    handled cleanly. Malformed dicts in new_items get logged + skipped
    rather than failing the whole call.
    """
    coverage = _ExtractionCoverage(source_chunks_total=1)
    settings = get_settings()
    client = get_anthropic()
    text = document_text[:_MAX_INPUT_CHARS]
    if len(document_text) > _MAX_INPUT_CHARS:
        log.warning(
            "compliance_matrix delta: %s truncated from %d to %d chars",
            filename,
            len(document_text),
            _MAX_INPUT_CHARS,
        )
        coverage.source_truncated = True
        coverage.flag("source_truncated")

    existing_items_text = _format_existing_items(existing_items or [])

    tool_input, usage = client.call_tool(
        model=settings.model_compliance_matrix,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": _USER_DELTA.format(
                    filename=filename,
                    existing_items_text=existing_items_text,
                    text=text,
                ),
            }
        ],
        tool=_TOOL_SPEC_DELTA,
        max_tokens=32000,
        agent_name="compliance_matrix_delta",
        proposal_id=proposal_id,
    )

    if not isinstance(tool_input, dict):
        log.error(
            "compliance_matrix delta: %s tool input is %s, not dict.",
            filename, type(tool_input).__name__,
        )
        coverage.fail_chunk("document", "malformed_tool_payload")
        tool_input = {}

    valid_fields = 0

    def _coerce_list(field_name: str) -> list:
        """Pull a field from tool_input and coerce to list (with JSON
        recovery fallbacks if the streaming parser handed back a string)."""
        nonlocal valid_fields
        if field_name not in tool_input:
            log.error(
                "compliance_matrix delta: %s omitted required field %r.",
                filename, field_name,
            )
            coverage.flag("malformed_tool_payload")
            return []
        raw = tool_input[field_name]
        if isinstance(raw, list):
            valid_fields += 1
            return raw
        if isinstance(raw, str):
            parsed = _parse_items_string(raw, filename=f"{filename} ({field_name})")
            if parsed is not None:
                valid_fields += 1
                return parsed
            log.error(
                "compliance_matrix delta: %s field %r un-parseable as list — treating as empty.",
                filename,
                field_name,
            )
            coverage.flag("malformed_tool_payload")
            return []
        log.warning(
            "compliance_matrix delta: %s field %r is %s — treating as empty.",
            filename,
            field_name,
            type(raw).__name__,
        )
        coverage.flag("malformed_tool_payload")
        return []

    raw_new = _coerce_list("new_items")
    raw_modified = _coerce_list("modified_items")
    raw_removed = _coerce_list("removed_items")

    if usage.get("stop_reason") == "max_tokens":
        coverage.response_truncated = True
        coverage.flag("response_truncated")

    # Bound the *entire* amendment delta, not just additions. A provider can
    # otherwise return an unbounded modified/removed list while still looking
    # complete to the apply layer. ``output_capped`` makes coverage partial,
    # and amendment ingestion fails closed before applying any sliced result.
    item_limit = max(0, int(max_items))
    total_raw_items = len(raw_new) + len(raw_modified) + len(raw_removed)
    if total_raw_items > item_limit:
        log.warning(
            "compliance_matrix delta: capping total delta at %d "
            "(got %d new/modified/removed item(s))",
            item_limit,
            total_raw_items,
        )
        coverage.output_capped = True
        coverage.flag("output_capped")
        remaining = item_limit
        raw_new = raw_new[:remaining]
        remaining -= len(raw_new)
        raw_modified = raw_modified[:remaining]
        remaining -= len(raw_modified)
        raw_removed = raw_removed[:remaining]

    source_pages = _source_pages(text)
    existing_text_by_id: dict[str, str] = {}
    for existing_item in existing_items:
        if not isinstance(existing_item, dict):
            continue
        existing_id = str(existing_item.get("requirement_id") or "").strip()
        existing_text = existing_item.get("requirement_text")
        if existing_id and isinstance(existing_text, str) and existing_text.strip():
            existing_text_by_id[existing_id] = _normalize_for_dedup(existing_text)
    existing_texts = set(existing_text_by_id.values())
    new_items: list[ExtractedComplianceItem] = []
    seen_new_text: set[str] = set()
    for index, item in enumerate(raw_new):
        try:
            parsed_item = _strict_extracted_item(item, source_pages=source_pages)
            normalized_text = _normalize_for_dedup(parsed_item.requirement_text)
            if normalized_text in seen_new_text:
                raise ValueError("duplicate new-item requirement_text")
            if normalized_text in existing_texts:
                raise ValueError("new item duplicates existing requirement_text")
            seen_new_text.add(normalized_text)
            new_items.append(parsed_item)
        except (TypeError, ValueError) as exc:
            coverage.malformed_items_skipped += 1
            coverage.flag("malformed_items_skipped")
            if "duplicate" in str(exc):
                coverage.flag("duplicate_delta_requirement")
            log.warning(
                "compliance_matrix delta: skipping malformed new_item %d: %s",
                index,
                exc,
            )

    modified_items: list[dict] = []
    allowed_existing_ids = {
        str(item.get("requirement_id") or "").strip()
        for item in existing_items
        if isinstance(item, dict) and str(item.get("requirement_id") or "").strip()
    }
    touched_existing_ids: set[str] = set()
    for index, item in enumerate(raw_modified):
        try:
            if not isinstance(item, dict):
                raise ValueError("item must be an object")
            existing_id = _nonempty_string(item.get("existing_id"), "existing_id")
            if existing_id not in allowed_existing_ids:
                raise ValueError(f"unknown existing_id {existing_id!r}")
            if existing_id in touched_existing_ids:
                raise ValueError(
                    f"conflicting delta operations for existing_id {existing_id!r}"
                )
            new_text = _nonempty_string(item.get("new_text"), "new_text")
            if not _requirement_is_grounded(
                new_text,
                source_pages=source_pages,
                source_page=None,
            ):
                raise ValueError("new_text is not grounded in the amendment source")
            if (
                existing_text_by_id.get(existing_id)
                and _normalize_for_dedup(new_text) == existing_text_by_id[existing_id]
            ):
                raise ValueError("no-op modification repeats existing requirement_text")
            change_summary = _nonempty_string(
                item.get("change_summary"),
                "change_summary",
            )
        except (TypeError, ValueError) as exc:
            coverage.malformed_items_skipped += 1
            coverage.flag("malformed_items_skipped")
            if "conflicting delta operations" in str(exc):
                coverage.flag("conflicting_delta_operations")
            if "no-op modification" in str(exc):
                coverage.flag("no_op_delta_modification")
            log.warning(
                "compliance_matrix delta: skipping malformed modified_item %d: %s",
                index,
                exc,
            )
            continue
        touched_existing_ids.add(existing_id)
        modified_items.append(
            {
                "existing_id": existing_id,
                "new_text": new_text,
                "change_summary": change_summary,
            }
        )

    removed_items: list[dict] = []
    for index, item in enumerate(raw_removed):
        try:
            if not isinstance(item, dict):
                raise ValueError("item must be an object")
            existing_id = _nonempty_string(item.get("existing_id"), "existing_id")
            if existing_id not in allowed_existing_ids:
                raise ValueError(f"unknown existing_id {existing_id!r}")
            if existing_id in touched_existing_ids:
                raise ValueError(
                    f"conflicting delta operations for existing_id {existing_id!r}"
                )
            reason = _nonempty_string(item.get("reason"), "reason")
        except (TypeError, ValueError) as exc:
            coverage.malformed_items_skipped += 1
            coverage.flag("malformed_items_skipped")
            if "conflicting delta operations" in str(exc):
                coverage.flag("conflicting_delta_operations")
            log.warning(
                "compliance_matrix delta: skipping malformed removed_item %d: %s",
                index,
                exc,
            )
            continue
        touched_existing_ids.add(existing_id)
        removed_items.append(
            {
                "existing_id": existing_id,
                "reason": reason,
            }
        )

    log.info(
        "compliance_matrix delta: %s -> %d new / %d modified / %d removed, %s stop=%s",
        filename,
        len(new_items),
        len(modified_items),
        len(removed_items),
        fmt_llm_usage(usage),
        usage.get("stop_reason"),
    )

    returned_count = len(new_items) + len(modified_items) + len(removed_items)
    if valid_fields == 0:
        coverage.fail_chunk("document", "malformed_tool_payload")
    elif coverage.response_truncated and returned_count == 0:
        coverage.fail_chunk("document", "response_truncated_without_items")
    else:
        coverage.source_chunks_completed = 1

    return ComplianceExtractionResult(
        new_items=new_items,
        modified_items=modified_items,
        removed_items=removed_items,
        **coverage.result_kwargs(returned_item_count=returned_count),
    )


def extract_compliance_items(
    *,
    document_text: str,
    filename: str,
    proposal_id: int,
    existing_items: list[dict] | None = None,
    delta_mode: bool = False,
    max_items: int = 500,
    max_workers: int | None = None,
) -> ComplianceExtractionResult:
    """Run the Compliance Matrix Agent against one RFP document.

    For documents <= CHUNK_THRESHOLD_CHARS (~100K), runs a single
    Sonnet call (existing behavior). For larger documents, splits
    on `--- Page N ---` boundaries into ~60K-char chunks, runs each
    in parallel via ThreadPoolExecutor, and merges + dedupes +
    renumbers requirement_ids.

    Synchronous — caller wraps in a thread if invoked from async
    context. The chunked path uses its own internal pool. Callers may pass a
    ``max_workers`` budget; otherwise it is capped at
    ``settings.shortfall_workers`` (default 6).

    When `delta_mode=True`, routes through `_extract_delta_chunk` once
    over the full document text (no chunking — amendment / Q&A docs are
    typically much smaller than original RFPs) and returns a result with
    `new_items` / `modified_items` / `removed_items` populated. The
    `items` field stays empty on the delta path; the apply layer
    (apply_amendment_delta) does the row-level mutations.
    """
    if not document_text or not document_text.strip():
        log.warning(
            "compliance_matrix: empty document text for %s",
            filename,
        )
        coverage = _ExtractionCoverage(source_chunks_total=1)
        coverage.fail_chunk("document", "empty_source")
        return ComplianceExtractionResult(
            **coverage.result_kwargs(returned_item_count=0),
        )

    if delta_mode:
        return _extract_delta_chunk(
            document_text=document_text,
            filename=filename,
            proposal_id=proposal_id,
            existing_items=existing_items or [],
            max_items=max_items,
        )

    if len(document_text) <= _CHUNK_THRESHOLD_CHARS:
        coverage = _ExtractionCoverage(source_chunks_total=1)
        items = _extract_one_chunk(
            document_text=document_text,
            filename=filename,
            proposal_id=proposal_id,
            max_items=max_items,
            coverage=coverage,
            coverage_label="document",
        )
        if not coverage.failed_chunk_labels:
            coverage.source_chunks_completed = 1
        return ComplianceExtractionResult(
            items=items,
            **coverage.result_kwargs(returned_item_count=len(items)),
        )

    # Chunked path — large doc.
    chunks = _split_text_by_pages(document_text)
    if len(chunks) <= 1:
        # Couldn't split (no page markers) — fall back to single call.
        coverage = _ExtractionCoverage(source_chunks_total=1)
        items = _extract_one_chunk(
            document_text=document_text,
            filename=filename,
            proposal_id=proposal_id,
            max_items=max_items,
            coverage=coverage,
            coverage_label="document",
        )
        if not coverage.failed_chunk_labels:
            coverage.source_chunks_completed = 1
        return ComplianceExtractionResult(
            items=items,
            **coverage.result_kwargs(returned_item_count=len(items)),
        )

    settings = get_settings()
    configured_workers = (
        settings.shortfall_workers if max_workers is None else max_workers
    )
    workers = max(1, min(len(chunks), int(configured_workers or 1)))
    log.info(
        "compliance_matrix: %s split into %d chunk(s) "
        "(input %d chars > threshold %d) — running %d worker(s) "
        "in parallel",
        filename,
        len(chunks),
        len(document_text),
        _CHUNK_THRESHOLD_CHARS,
        workers,
    )

    # Result slot per chunk so we can stitch back in document order
    # regardless of completion order.
    chunk_results: list[list[ExtractedComplianceItem]] = [
        [] for _ in chunks
    ]
    chunk_coverages = [
        _ExtractionCoverage(source_chunks_total=1) for _ in chunks
    ]
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"compmat-chunk-{proposal_id}",
    ) as ex:
        future_to_idx = {
            ex.submit(
                _extract_one_chunk,
                document_text=chunk,
                filename=f"{filename} (chunk {idx + 1}/{len(chunks)})",
                proposal_id=proposal_id,
                max_items=max_items,
                coverage=chunk_coverages[idx],
                coverage_label=f"chunk {idx + 1}/{len(chunks)}",
            ): idx
            for idx, chunk in enumerate(chunks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                chunk_results[idx] = future.result()
                if not chunk_coverages[idx].failed_chunk_labels:
                    chunk_coverages[idx].source_chunks_completed = 1
            except Exception:
                chunk_coverages[idx].fail_chunk(
                    f"chunk {idx + 1}/{len(chunks)}",
                    "chunk_call_failed",
                )
                log.exception(
                    "compliance_matrix: chunk %d/%d failed for %s — skipping; remaining chunks continue.",
                    idx + 1,
                    len(chunks),
                    filename,
                )

    coverage = _ExtractionCoverage()
    for chunk_coverage in chunk_coverages:
        coverage.merge(chunk_coverage)

    # Merge in document order, dedupe by normalized requirement_text,
    # then renumber requirement_id sequentially. Each chunk's per-call
    # numbering (REQ-001 within chunk N) collides across chunks; the
    # renumber pass produces a single global sequence the validator
    # and downstream agents reference.
    seen_keys: set[str] = set()
    merged: list[ExtractedComplianceItem] = []
    n_dupes = 0
    for items in chunk_results:
        for item in items:
            key = _normalize_for_dedup(item.requirement_text)
            if not key:
                coverage.malformed_items_skipped += 1
                coverage.flag("malformed_items_skipped")
                continue
            if key in seen_keys:
                n_dupes += 1
                continue
            seen_keys.add(key)
            merged.append(item)

    if n_dupes:
        log.info(
            "compliance_matrix: %s — deduped %d cross-chunk duplicate(s) before renumbering",
            filename,
            n_dupes,
        )
    if len(merged) > max_items:
        log.warning(
            "compliance_matrix: %s — capping merged items at %d (had %d after dedupe)",
            filename,
            max_items,
            len(merged),
        )
        coverage.output_capped = True
        coverage.flag("output_capped")
        merged = merged[:max_items]

    # Renumber requirement_id sequentially in document order so
    # downstream code (validator, persistence, UI) sees a single
    # uniform REQ-001..REQ-N sequence per document.
    for i, item in enumerate(merged, 1):
        item.requirement_id = f"REQ-{i:03d}"

    log.info(
        "compliance_matrix: %s — merged %d item(s) from %d chunk(s)",
        filename,
        len(merged),
        len(chunks),
    )
    return ComplianceExtractionResult(
        items=merged,
        **coverage.result_kwargs(returned_item_count=len(merged)),
    )
