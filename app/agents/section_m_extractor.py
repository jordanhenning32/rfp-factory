"""Section M Evaluation-Criteria Extractor — extract evaluation factors,
weights, and scoring scales from the portions of an RFP that describe how
proposals will be scored (Section M / "Evaluation Factors for Award" /
FAR-style "how we'll evaluate" sections).

Mirrors the compliance_matrix.py pattern:
- Sonnet + tool-use for guaranteed structured output
- Verbatim extraction only — no invented weights or sub-factors
- Page-boundary chunking when the document exceeds _CHUNK_THRESHOLD_CHARS
- Malformed-JSON recovery via json_repair
- Parallel chunk dispatch via ThreadPoolExecutor

Emits a single EvaluationCriteria object per RFP package:
  evaluation_method + factors (with weights, scoring scales,
  evidence_required, sub-factors) + section_l_to_m_map (REQ-ID →
  factor_id cross-references) + verbatim trade_off_language /
  lowest_price_clause + agent extraction_notes.

The caller (app/jobs/intake._run_section_m_extractor) persists the result
as JSON on proposals.evaluation_criteria_json (migration 0032).
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import json_repair

from app.config import get_settings
from app.services.llm import fmt_llm_usage, get_anthropic

log = logging.getLogger(__name__)


_TOOL_SPEC: dict = {
    "name": "report_evaluation_criteria",
    "description": (
        "Report the structured evaluation criteria extracted from the RFP. "
        "Call exactly once with everything you extracted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "evaluation_method": {
                "type": "string",
                "enum": ["best_value", "lpta", "trade_off", "unknown"],
                "description": (
                    "How the buyer will select the awardee: best_value = "
                    "buyer may trade price for non-price factors; lpta = "
                    "Lowest Price Technically Acceptable; trade_off = explicit "
                    "trade-off process; unknown = RFP is silent or ambiguous."
                ),
            },
            "factors": {
                "type": "array",
                "description": "Evaluation factors. May be empty when the RFP does not enumerate them.",
                "items": {
                    "type": "object",
                    "properties": {
                        "factor_id": {
                            "type": "string",
                            "description": "Sequential label, e.g. 'F1', 'F2'.",
                        },
                        "factor_name": {
                            "type": "string",
                            "description": "Name of the factor as stated in the RFP.",
                        },
                        "weight_pct": {
                            "type": ["number", "null"],
                            "description": "Numeric percentage 0-100. null when not disclosed.",
                        },
                        "weight_descriptive": {
                            "type": ["string", "null"],
                            "description": (
                                "Descriptive weight when only prose is given, "
                                "e.g. 'most important', 'equally weighted'."
                            ),
                        },
                        "scoring_scale": {
                            "type": ["string", "null"],
                            "description": (
                                "Adjectival or numeric scale, e.g. "
                                "'Exceptional/Acceptable/Marginal/Unacceptable'."
                            ),
                        },
                        "evidence_required": {
                            "type": ["string", "null"],
                            "description": "What the RFP says the offeror must submit for this factor.",
                        },
                        "subfactors": {
                            "type": "array",
                            "description": "Sub-factors when the RFP states them. Empty when none.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "weight_pct": {"type": ["number", "null"]},
                                    "notes": {"type": ["string", "null"]},
                                },
                                "required": ["name"],
                            },
                        },
                    },
                    "required": ["factor_id", "factor_name"],
                },
            },
            "section_l_to_m_map": {
                "type": "object",
                "description": (
                    "Map from compliance item IDs (REQ-NNN) to lists of factor IDs "
                    "(e.g. ['F1', 'F1.1']). Populated ONLY from explicit RFP cross-references. "
                    "Empty object when no cross-references exist."
                ),
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "trade_off_language": {
                "type": ["string", "null"],
                "description": "VERBATIM quote of any trade-off / best-value wording from the RFP.",
            },
            "lowest_price_clause": {
                "type": ["string", "null"],
                "description": "VERBATIM quote of the LPTA clause, if present.",
            },
            "extraction_notes": {
                "type": ["string", "null"],
                "description": (
                    "Agent commentary on confidence and gaps (e.g. 'Factor 3 has no weight "
                    "disclosed', 'No section_l_to_m_map possible'). Under 600 chars."
                ),
            },
        },
        "required": ["evaluation_method", "factors", "section_l_to_m_map", "extraction_notes"],
    },
}


_SYSTEM = """You are a Section M (evaluation-criteria) extractor for U.S. government RFP responses. Your job is to read an RFP package and produce a STRUCTURED reading of how the buyer will score proposals.

Hard rules:
1. Extract ONLY criteria the RFP explicitly states. NEVER invent weights, scales, or sub-factors. If a field is not present in the source text, output null / empty rather than guessing.
2. If weights are not given, report `weight_pct: null` and use `weight_descriptive` (e.g., 'most important', 'equally weighted', 'all factors carry equal weight').
3. If the RFP says best-value but does not enumerate factors, set `evaluation_method='best_value'` and leave `factors=[]` — explain the situation in `extraction_notes`.
4. Map each compliance item ID (REQ-NNN) to one or more `factor_id` values ONLY by reading the RFP's own cross-references (e.g., 'Section L paragraph 3.2 corresponds to Factor 1' or 'Submit your Technical Approach as required by L.3 — it will be evaluated under Factor 1'). Do NOT guess mappings the RFP doesn't state. Empty map is the correct answer when no cross-references exist.
5. `trade_off_language` and `lowest_price_clause` must be VERBATIM quotes from the source text. NO paraphrase. NO modernization. NO acronym expansion. Use the exact wording the RFP uses, even if it sounds dated or jargon-heavy.
6. Set `evaluation_method`: `lpta` when the RFP awards to the Lowest Price Technically Acceptable proposal; `best_value` when the buyer reserves the right to trade price for non-price factors; `trade_off` when the RFP explicitly describes a trade-off process with price vs. non-price factors; `unknown` when the language is ambiguous or the section is silent.
7. Sub-factors only when the RFP states them — do NOT decompose factors yourself.
8. `extraction_notes` is for transparency about confidence and gaps (e.g., 'Factor 3 has no weight disclosed but is described as second-most-important', 'No section_l_to_m_map possible — RFP does not cross-reference Section L items to factors'). Keep it under 600 chars.

Call the report_evaluation_criteria tool exactly once with everything you extracted. Do not include any commentary outside the tool call."""


_USER = """Extract the buyer's evaluation criteria from this RFP document.

DOCUMENT FILENAME: {filename}

COMPLIANCE ITEMS ALREADY EXTRACTED (use these REQ-IDs for the section_l_to_m_map; the IDs and short excerpts give you the Section L item-to-factor cross-references you need to populate the map):
{compliance_items_block}

DOCUMENT TEXT:
{text}

Call the report_evaluation_criteria tool with the complete structured extraction."""


@dataclass
class EvaluationCriteria:
    """Structured result from the Section M extractor."""

    evaluation_method: str  # "best_value" | "lpta" | "trade_off" | "unknown"
    factors: list[dict]  # raw list-of-dicts; keep raw dicts to preserve nested subfactors
    section_l_to_m_map: dict[str, list[str]]
    trade_off_language: str | None
    lowest_price_clause: str | None
    extraction_notes: str | None

    def as_dict(self) -> dict:
        """Return a JSON-serializable dict ready for json.dumps."""
        return {
            "evaluation_method": self.evaluation_method,
            "factors": self.factors,
            "section_l_to_m_map": self.section_l_to_m_map,
            "trade_off_language": self.trade_off_language,
            "lowest_price_clause": self.lowest_price_clause,
            "extraction_notes": self.extraction_notes,
        }

    @classmethod
    def from_tool_input(cls, raw: dict) -> EvaluationCriteria:
        """Build from the agent's tool_input dict, defensively coercing missing fields."""
        method = raw.get("evaluation_method", "unknown")
        if method not in ("best_value", "lpta", "trade_off", "unknown"):
            method = "unknown"

        factors = raw.get("factors") or []
        if not isinstance(factors, list):
            factors = []
        # Ensure each factor is a dict with required keys
        clean_factors = []
        for f in factors:
            if not isinstance(f, dict):
                continue
            factor = {
                "factor_id": str(f.get("factor_id") or ""),
                "factor_name": str(f.get("factor_name") or ""),
                "weight_pct": f.get("weight_pct"),
                "weight_descriptive": f.get("weight_descriptive"),
                "scoring_scale": f.get("scoring_scale"),
                "evidence_required": f.get("evidence_required"),
                "subfactors": f.get("subfactors") or [],
            }
            if not isinstance(factor["subfactors"], list):
                factor["subfactors"] = []
            if factor["factor_id"] or factor["factor_name"]:
                clean_factors.append(factor)

        sl_map = raw.get("section_l_to_m_map") or {}
        if not isinstance(sl_map, dict):
            sl_map = {}
        # Ensure all values are lists of strings
        clean_map: dict[str, list[str]] = {}
        for k, v in sl_map.items():
            if isinstance(v, list):
                clean_map[str(k)] = [str(x) for x in v]

        return cls(
            evaluation_method=method,
            factors=clean_factors,
            section_l_to_m_map=clean_map,
            trade_off_language=raw.get("trade_off_language") or None,
            lowest_price_clause=raw.get("lowest_price_clause") or None,
            extraction_notes=raw.get("extraction_notes") or None,
        )


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


def _format_compliance_items_for_prompt(items: list[dict] | None) -> str:
    """Render compliance items as a compact block for the agent prompt.

    Empty list / None → returns a note that no items are available.
    Otherwise renders each as "REQ-NNN: <first 200 chars of text>",
    capped at 30 items (first ~6000 chars) so we don't blow context.
    """
    if not items:
        return "(none — no compliance matrix items available; produce an empty section_l_to_m_map)"
    lines = []
    cap = 30
    for item in items[:cap]:
        req_id = item.get("requirement_id", "REQ-???")
        req_text = (item.get("requirement_text") or "")[:200]
        lines.append(f"{req_id}: {req_text}")
    if len(items) > cap:
        lines.append(f"… and {len(items) - cap} more")
    return "\n".join(lines)


def _parse_tool_input_string(raw: str, *, filename: str, key: str) -> dict | list | None:
    """Try to recover a structured object from a string that the SDK delivered
    in place of the expected structured tool input. Try json.loads, then
    json_repair.loads. Returns the parsed object or None.
    `key` is just a label for log messages."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        log.warning(
            "section_m_extractor: %s '%s' was a JSON-encoded string — recovered via json.loads.",
            filename,
            key,
        )
        return parsed

    try:
        repaired = json_repair.loads(raw)
    except Exception:  # noqa: BLE001 — defend against any repair-time crash
        return None
    if isinstance(repaired, (dict, list)):
        log.warning(
            "section_m_extractor: %s '%s' had malformed JSON — recovered via json_repair.",
            filename,
            key,
        )
        return repaired
    return None


def _dump_failed_payload(
    *,
    raw: str,
    filename: str,
    parse_error: str,
) -> Path | None:
    """Write a malformed tool-input payload to disk for operator inspection.
    Returns the dump path on success, None on failure."""
    try:
        dump_dir = Path("data/debug/section_m_extractor")
        dump_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_name = re.sub(r"[^\w-]", "_", filename)[:80]
        dump_path = dump_dir / f"{ts}_{safe_name}.txt"
        header = (
            f"# parse_error: {parse_error}\n"
            f"# filename: {filename}\n"
            f"# raw_len: {len(raw)}\n"
            f"# --- raw tool input below this line ---\n"
        )
        dump_path.write_text(header + raw, encoding="utf-8")
        return dump_path
    except Exception:  # noqa: BLE001 — best-effort debug aid
        log.exception("section_m_extractor: failed to write debug dump")
        return None


def _extract_one_chunk(
    *,
    document_text: str,
    filename: str,
    proposal_id: int | None,
    compliance_items_block: str,
) -> EvaluationCriteria:
    """Run a single Sonnet tool-use call against one chunk of document text.

    On malformed tool input: attempts json_repair recovery; if recovery
    fails, returns an empty EvaluationCriteria with explanatory notes.
    We do NOT recurse-split on Section M because the tool returns a single
    object, not a list — halving the chunk doesn't help merge.
    """
    settings = get_settings()
    client = get_anthropic()

    user_prompt = _USER.format(
        filename=filename,
        text=document_text[:_MAX_INPUT_CHARS],
        compliance_items_block=compliance_items_block,
    )

    try:
        tool_input, usage = client.call_tool(
            model=settings.model_compliance_matrix,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            tool=_TOOL_SPEC,
            max_tokens=16000,
            agent_name="section_m_extractor",
            proposal_id=proposal_id,
        )
    except Exception:
        log.exception(
            "section_m_extractor: LLM call failed for %s",
            filename,
        )
        return EvaluationCriteria(
            evaluation_method="unknown",
            factors=[],
            section_l_to_m_map={},
            trade_off_language=None,
            lowest_price_clause=None,
            extraction_notes="LLM call failed — see logs.",
        )

    log.info(
        "section_m_extractor: %s -> %s",
        filename,
        fmt_llm_usage(usage),
    )

    # The SDK returns the tool input directly as a dict when streaming
    # tool-use. Handle the case where it arrives as a string.
    if isinstance(tool_input, str):
        recovered = _parse_tool_input_string(tool_input, filename=filename, key="tool_input")
        if isinstance(recovered, dict):
            tool_input = recovered
        else:
            dump_path = _dump_failed_payload(
                raw=tool_input,
                filename=filename,
                parse_error="tool_input was a string, json_repair could not produce a dict",
            )
            log.error(
                "section_m_extractor: unrecoverable parse failure for %s — "
                "dump at %s. Returning empty criteria.",
                filename,
                dump_path,
            )
            return EvaluationCriteria(
                evaluation_method="unknown",
                factors=[],
                section_l_to_m_map={},
                trade_off_language=None,
                lowest_price_clause=None,
                extraction_notes=f"Parse failure (string tool input, unrecoverable) — dump: {dump_path}",
            )

    if not isinstance(tool_input, dict):
        log.error(
            "section_m_extractor: unexpected tool_input type %s for %s",
            type(tool_input).__name__,
            filename,
        )
        return EvaluationCriteria(
            evaluation_method="unknown",
            factors=[],
            section_l_to_m_map={},
            trade_off_language=None,
            lowest_price_clause=None,
            extraction_notes=f"Unexpected tool input type: {type(tool_input).__name__}",
        )

    return EvaluationCriteria.from_tool_input(tool_input)


def _merge_chunk_results(results: list[EvaluationCriteria]) -> EvaluationCriteria:
    """Merge multiple chunk results into a single EvaluationCriteria.

    Merge rules:
    - evaluation_method: first non-"unknown" wins; else "unknown".
    - factors: concatenate, de-dupe by lowered factor_name. First occurrence wins.
    - section_l_to_m_map: union; same REQ-ID → union of factor-ID lists.
    - trade_off_language / lowest_price_clause: first non-null wins.
    - extraction_notes: joined with " | ", capped at 600 chars.
    """
    if not results:
        return EvaluationCriteria(
            evaluation_method="unknown",
            factors=[],
            section_l_to_m_map={},
            trade_off_language=None,
            lowest_price_clause=None,
            extraction_notes="No chunks processed.",
        )

    method = "unknown"
    for r in results:
        if r.evaluation_method != "unknown":
            method = r.evaluation_method
            break

    seen_names: set[str] = set()
    merged_factors: list[dict] = []
    for r in results:
        for f in r.factors:
            key = (f.get("factor_name") or "").lower().strip()
            if key and key not in seen_names:
                seen_names.add(key)
                merged_factors.append(f)

    merged_map: dict[str, list[str]] = {}
    for r in results:
        for req_id, fids in r.section_l_to_m_map.items():
            existing = merged_map.get(req_id, [])
            for fid in fids:
                if fid not in existing:
                    existing.append(fid)
            merged_map[req_id] = existing

    trade_off = None
    for r in results:
        if r.trade_off_language:
            trade_off = r.trade_off_language
            break

    lpta = None
    for r in results:
        if r.lowest_price_clause:
            lpta = r.lowest_price_clause
            break

    notes_parts = [r.extraction_notes for r in results if r.extraction_notes]
    notes = " | ".join(notes_parts)[:600] if notes_parts else None

    # Renumber factor_ids sequentially F1, F2, ... and rewrite section_l_to_m_map
    old_to_new: dict[str, str] = {}
    for idx, f in enumerate(merged_factors, start=1):
        new_id = f"F{idx}"
        old_id = f.get("factor_id") or new_id
        old_to_new[old_id] = new_id
        f["factor_id"] = new_id

    # Rewrite the map using new factor IDs
    rewritten_map: dict[str, list[str]] = {}
    for req_id, fids in merged_map.items():
        new_fids = []
        for fid in fids:
            new_fids.append(old_to_new.get(fid, fid))
        rewritten_map[req_id] = new_fids

    return EvaluationCriteria(
        evaluation_method=method,
        factors=merged_factors,
        section_l_to_m_map=rewritten_map,
        trade_off_language=trade_off,
        lowest_price_clause=lpta,
        extraction_notes=notes,
    )


def extract_evaluation_criteria(
    *,
    proposal_id: int | None,
    document_text: str,
    filename: str,
    compliance_items: list[dict] | None = None,
) -> EvaluationCriteria:
    """Public entrypoint — extract evaluation criteria from document text.

    Args:
        proposal_id: Proposal PK for cost tracking; pass None before the
            proposal row exists.
        document_text: Full document text (may be concatenated multi-file).
        filename: Primary filename (for logging / debug dumps).
        compliance_items: List of {"requirement_id": str, "requirement_text": str}
            dicts from the compliance matrix; used to populate section_l_to_m_map.

    Returns:
        A single EvaluationCriteria. Caller persists to evaluation_criteria_json.
    """
    text = (document_text or "").strip()
    if not text:
        return EvaluationCriteria(
            evaluation_method="unknown",
            factors=[],
            section_l_to_m_map={},
            trade_off_language=None,
            lowest_price_clause=None,
            extraction_notes="empty document text",
        )

    compliance_items_block = _format_compliance_items_for_prompt(compliance_items)

    if len(text) <= _CHUNK_THRESHOLD_CHARS:
        return _extract_one_chunk(
            document_text=text,
            filename=filename,
            proposal_id=proposal_id,
            compliance_items_block=compliance_items_block,
        )

    # Large document — split and process in parallel
    chunks = _split_text_by_pages(text)
    settings = get_settings()
    max_workers = min(len(chunks), settings.shortfall_workers or 1)

    results: list[EvaluationCriteria] = [None] * len(chunks)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(
                _extract_one_chunk,
                document_text=chunk,
                filename=filename,
                proposal_id=proposal_id,
                compliance_items_block=compliance_items_block,
            ): i
            for i, chunk in enumerate(chunks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception:
                log.exception(
                    "section_m_extractor: chunk %d/%d failed for %s",
                    idx + 1,
                    len(chunks),
                    filename,
                )
                results[idx] = EvaluationCriteria(
                    evaluation_method="unknown",
                    factors=[],
                    section_l_to_m_map={},
                    trade_off_language=None,
                    lowest_price_clause=None,
                    extraction_notes=f"chunk {idx + 1} failed — see logs.",
                )

    return _merge_chunk_results(results)
