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

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import json_repair

from app.config import get_settings
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
    """

    items: list[ExtractedComplianceItem] = field(default_factory=list)
    new_items: list[ExtractedComplianceItem] = field(default_factory=list)
    modified_items: list[dict] = field(default_factory=list)
    removed_items: list[dict] = field(default_factory=list)


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
_PAGE_MARKER_RE = re.compile(r"^--- Page \d+ ---$", re.MULTILINE)


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
    chunk_start = page_starts[0]
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
    """Write a malformed tool-input payload to disk so we can inspect
    what's breaking. Returns the dump path on success, None on failure.
    Per-invocation timestamped so concurrent failures don't collide."""
    try:
        dump_dir = Path("data/debug/compliance_matrix")
        dump_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_name = re.sub(r"[^\w-]", "_", filename)[:80]
        dump_path = dump_dir / f"{ts}_{safe_name}_d{recursion_depth}.txt"
        header = (
            f"# parse_error: {parse_error}\n"
            f"# filename: {filename}\n"
            f"# recursion_depth: {recursion_depth}\n"
            f"# raw_len: {len(raw)}\n"
            f"# --- raw items field below this line ---\n"
        )
        dump_path.write_text(header + raw, encoding="utf-8")
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
) -> list[ExtractedComplianceItem]:
    """Single Sonnet call against one chunk (or the whole doc when
    it fits inside the threshold). Caller handles dispatch + merge.

    On malformed-JSON failures (model produced unescaped chars in a
    verbatim quote and the streaming parser gave us a raw string),
    halve the chunk on page boundaries and recurse. Bounded by
    `recursion_depth` to avoid runaway costs on a pathological chunk.
    """
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

    tool_input, usage = client.call_tool(
        model=settings.model_compliance_matrix,  # Sonnet 4.6 by default
        system=_SYSTEM,
        messages=[{"role": "user", "content": _USER.format(filename=filename, text=text)}],
        tool=_TOOL_SPEC,
        max_tokens=32000,
        agent_name="compliance_matrix",
        proposal_id=proposal_id,
    )

    raw_items = tool_input.get("items", [])
    needs_recovery = False
    recovery_reason = ""

    # Anthropic streaming tool-use can return `items` as a raw JSON
    # *string* instead of a parsed list. Two seen modes:
    #   (a) the array was JSON-encoded as a string — `json.loads` recovers
    #   (b) the model emitted JSON with unescaped chars in string values
    #       (commonly literal `"` chars in verbatim quotes from the RFP,
    #       e.g. ("Warranty Period")) and the SDK's partial-JSON parser
    #       left the un-parseable subtree as a raw string — `json_repair`
    #       recovers since the lib was built for exactly this.
    # Only fall through to split-and-retry if BOTH parsers fail.
    if isinstance(raw_items, str):
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
    elif not isinstance(raw_items, list):
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
    if not raw_items and usage.get("stop_reason") == "max_tokens":
        log.error(
            "compliance_matrix: %s returned 0 items AND hit max_tokens — "
            "the response was truncated mid-JSON. Increase max_tokens further.",
            filename,
        )

    if len(raw_items) > max_items:
        log.warning(
            "compliance_matrix: capping items at %d (got %d)",
            max_items,
            len(raw_items),
        )
        raw_items = raw_items[:max_items]

    extracted: list[ExtractedComplianceItem] = []
    for item in raw_items:
        try:
            extracted.append(
                ExtractedComplianceItem(
                    requirement_id=str(item["requirement_id"]),
                    requirement_text=str(item["requirement_text"]),
                    requirement_type=str(item["requirement_type"]),
                    category=str(item["category"]),
                    source_section=item.get("source_section"),
                    source_page=item.get("source_page"),
                    weight=item.get("weight"),
                )
            )
        except (KeyError, TypeError) as exc:
            log.warning(
                "compliance_matrix: skipping malformed item %r: %s",
                item,
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

    def _coerce_list(field_name: str) -> list:
        """Pull a field from tool_input and coerce to list (with JSON
        recovery fallbacks if the streaming parser handed back a string)."""
        raw = tool_input.get(field_name, [])
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            parsed = _parse_items_string(raw, filename=f"{filename} ({field_name})")
            if parsed is not None:
                return parsed
            log.error(
                "compliance_matrix delta: %s field %r un-parseable as list — treating as empty.",
                filename,
                field_name,
            )
            return []
        log.warning(
            "compliance_matrix delta: %s field %r is %s — treating as empty.",
            filename,
            field_name,
            type(raw).__name__,
        )
        return []

    raw_new = _coerce_list("new_items")
    raw_modified = _coerce_list("modified_items")
    raw_removed = _coerce_list("removed_items")

    if len(raw_new) > max_items:
        log.warning(
            "compliance_matrix delta: capping new_items at %d (got %d)",
            max_items,
            len(raw_new),
        )
        raw_new = raw_new[:max_items]

    new_items: list[ExtractedComplianceItem] = []
    for item in raw_new:
        try:
            new_items.append(
                ExtractedComplianceItem(
                    requirement_id=str(item.get("requirement_id") or ""),
                    requirement_text=str(item["requirement_text"]),
                    requirement_type=str(item["requirement_type"]),
                    category=str(item["category"]),
                    source_section=item.get("source_section"),
                    source_page=item.get("source_page"),
                    weight=item.get("weight"),
                )
            )
        except (KeyError, TypeError) as exc:
            log.warning(
                "compliance_matrix delta: skipping malformed new_item %r: %s",
                item,
                exc,
            )

    modified_items: list[dict] = []
    for item in raw_modified:
        if not isinstance(item, dict):
            log.warning(
                "compliance_matrix delta: skipping non-dict modified_item %r",
                item,
            )
            continue
        if not item.get("existing_id") or not item.get("new_text"):
            log.warning(
                "compliance_matrix delta: skipping modified_item missing required keys: %r",
                item,
            )
            continue
        modified_items.append(
            {
                "existing_id": str(item["existing_id"]),
                "new_text": str(item["new_text"]),
                "change_summary": str(item.get("change_summary") or ""),
            }
        )

    removed_items: list[dict] = []
    for item in raw_removed:
        if not isinstance(item, dict):
            log.warning(
                "compliance_matrix delta: skipping non-dict removed_item %r",
                item,
            )
            continue
        if not item.get("existing_id"):
            log.warning(
                "compliance_matrix delta: skipping removed_item missing existing_id: %r",
                item,
            )
            continue
        removed_items.append(
            {
                "existing_id": str(item["existing_id"]),
                "reason": str(item.get("reason") or ""),
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

    return ComplianceExtractionResult(
        new_items=new_items,
        modified_items=modified_items,
        removed_items=removed_items,
    )


def extract_compliance_items(
    *,
    document_text: str,
    filename: str,
    proposal_id: int,
    existing_items: list[dict] | None = None,
    delta_mode: bool = False,
    max_items: int = 500,
) -> ComplianceExtractionResult:
    """Run the Compliance Matrix Agent against one RFP document.

    For documents <= CHUNK_THRESHOLD_CHARS (~100K), runs a single
    Sonnet call (existing behavior). For larger documents, splits
    on `--- Page N ---` boundaries into ~60K-char chunks, runs each
    in parallel via ThreadPoolExecutor, and merges + dedupes +
    renumbers requirement_ids.

    Synchronous — caller wraps in a thread if invoked from async
    context. The chunked path uses its own internal pool (capped at
    settings.shortfall_workers, defaulting to 6).

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
        return ComplianceExtractionResult()

    if delta_mode:
        return _extract_delta_chunk(
            document_text=document_text,
            filename=filename,
            proposal_id=proposal_id,
            existing_items=existing_items or [],
            max_items=max_items,
        )

    if len(document_text) <= _CHUNK_THRESHOLD_CHARS:
        items = _extract_one_chunk(
            document_text=document_text,
            filename=filename,
            proposal_id=proposal_id,
            max_items=max_items,
        )
        return ComplianceExtractionResult(items=items)

    # Chunked path — large doc.
    chunks = _split_text_by_pages(document_text)
    if len(chunks) <= 1:
        # Couldn't split (no page markers) — fall back to single call.
        items = _extract_one_chunk(
            document_text=document_text,
            filename=filename,
            proposal_id=proposal_id,
            max_items=max_items,
        )
        return ComplianceExtractionResult(items=items)

    settings = get_settings()
    workers = max(1, min(len(chunks), int(settings.shortfall_workers or 1)))
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
    chunk_results: list[list[ExtractedComplianceItem]] = [[] for _ in chunks]
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
            ): idx
            for idx, chunk in enumerate(chunks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                chunk_results[idx] = future.result()
            except Exception:
                log.exception(
                    "compliance_matrix: chunk %d/%d failed for %s — skipping; remaining chunks continue.",
                    idx + 1,
                    len(chunks),
                    filename,
                )

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
    return ComplianceExtractionResult(items=merged)
