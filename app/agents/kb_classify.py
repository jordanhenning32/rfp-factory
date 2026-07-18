"""KB document classifier.

Haiku-class call that picks the right KbDocumentClass for a freshly-staged
file and suggests tags. Runs once per upload session (first file only),
just like the RFP intake metadata extractor.

Critical distinctions the classifier must get right:
- past_performance (backward-looking) vs prior_proposal (forward-looking)
- past_performance_won vs past_performance_subbed (role of the firm)
- compliance_evidence (proves facts about the firm) vs agency_context
  (describes the customer)

These distinctions drive citation legitimacy downstream — getting them
wrong means the firm could cite a pending proposal as completed work,
which Reviewer A is supposed to flag in Phase 1 weeks 9-10.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.core.enums import KbDocumentClass
from app.services.llm import fmt_llm_usage, get_anthropic
from app.services.pdf_extract import extract_pdf_text

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "classify_kb_document",
    "description": "Classify a knowledge-base document and suggest retrieval tags.",
    "input_schema": {
        "type": "object",
        "properties": {
            "document_class": {
                "type": "string",
                "enum": [c.value for c in KbDocumentClass],
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "rationale": {
                "type": "string",
                "description": "1-2 sentences explaining the choice.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "5-10 useful retrieval tags: agency names, NAICS codes, "
                    "scope domains, project names, contract types."
                ),
            },
        },
        "required": ["document_class", "confidence"],
    },
}


_SYSTEM = """You classify documents for an RFP-response firm's knowledge base.

Classes:
- corporate: capability statement, company-wide bio, website content describing the FIRM as a whole.
- personnel: resume or structured bio for ONE PERSON written by/for that person.
- past_performance_won: COMPLETED work the firm did as PRIME contractor (backward-looking, written by the firm).
- past_performance_subbed: COMPLETED work the firm did as SUBCONTRACTOR to a named prime (backward-looking, written by the firm).
- references_project: a CUSTOMER-authored reference letter, customer contact sheet, or testimonial about a project the firm delivered. Distinct from past_performance because the AUTHOR is the customer, not the firm.
- references_personnel: a reference letter or endorsement written by a third party (former manager, peer, customer) about a SINGLE named person. Often a "letter of recommendation" or named-referee contact card.
- prior_proposal_pending: a PROPOSAL the firm submitted (forward-looking: "we propose to..."), outcome unknown.
- prior_proposal_won: a proposal the firm submitted that explicitly states it WON.
- prior_proposal_lost: a proposal the firm submitted that explicitly states it LOST.
- compliance_evidence: insurance certificate, financial statement, certification proof — documents that PROVE FACTS about the firm.
- agency_context: third-party document about a customer agency (strategic plan, GAO/OIG report, Q&A logs).
- boilerplate: reusable standard language the firm plugs INTO proposals (quality plan template, risk framework, etc.).
- procurement_craft: META-guidance ABOUT how to write winning proposals — public-domain proposal-writing guides, government procurement institute publications, APMP/Shipley framework summaries, evaluator-psychology references. NOT specific to any one customer; NOT reusable text the firm plugs into proposals; NOT about the firm itself. The Writer Team uses these to shape structure and evaluator-alignment decisions.

Hard distinctions to get right:
1. past_performance is BACKWARD-looking ("we delivered...", "completed in 2023"). prior_proposal is FORWARD-looking ("we propose to...", "our approach will be").
2. past_performance_subbed vs past_performance_won — be CONSERVATIVE here. Misclassifying subcontract work as prime work is a procurement-integrity violation. Rules:
   - If the document mentions the firm as "subcontractor to [prime]", "via [prime]", "under [prime]", or describes federal/state contracts where the firm was NOT the prime awardee → past_performance_subbed.
   - If the document covers MULTIPLE projects with mixed roles (some prime, some sub) → use past_performance_subbed (the safer label; the agent reading it can pick out per-row roles).
   - past_performance_won is only correct when the firm was clearly the PRIME contractor on every project described.
3. PERSONNEL is for ONE PERSON. If the document covers multiple people (a personnel exhibit, a bios appendix, a staff roster, a personnel summary table with rows for many people) → classify as CORPORATE (firm-wide personnel exhibit), not personnel.
4. COMPLIANCE_EVIDENCE catches legal artifacts about the firm: insurance certificates, leases, signed contracts/agreements, business licenses, certificates of authority, W-9s, financial statements. If a document is a SIGNED LEGAL INSTRUMENT or PROOF DOCUMENT about the firm, it's compliance_evidence even if it's "about the company" in a general sense — corporate is for marketing/positioning material (capability statements, company bios, pitch decks).
5. references_project vs past_performance: a reference is written BY THE CUSTOMER about the firm ("this contractor delivered excellent work...", letter on customer letterhead, customer POC contact card). past_performance is written BY THE FIRM about its own work.
6. references_personnel vs personnel: a personnel doc is the person's own resume/bio. A references_personnel doc is written ABOUT a person by someone else (manager letter, peer recommendation, named-referee contact info).
7. prior_proposal_pending is the safe default for proposal documents whose outcome is not stated. Only mark won/lost if the document explicitly says so.
8. compliance_evidence proves facts about the firm (insurance, financials, registration). agency_context describes the CUSTOMER, not the firm.
9. A resume covering ONE person → personnel. A bio about the company as a whole → corporate.
10. procurement_craft vs boilerplate: procurement_craft is META ("here's how to write a strong technical approach section"). boilerplate is OBJECT-LEVEL reusable text ("here is our standard quality assurance plan to drop in"). When in doubt, look at WHO the document is for — if it's instructional for the proposal writer, that's procurement_craft.
11. procurement_craft vs agency_context: procurement_craft is generic across customers. agency_context is about ONE specific agency.

Return one class with confidence (high if very clear from the text, low if uncertain), a 1-2 sentence rationale, and 5-10 retrieval tags."""


@dataclass
class ClassificationResult:
    document_class: KbDocumentClass
    confidence: str  # "high" | "medium" | "low"
    rationale: str | None
    tags: list[str]


def _extract_text_for_classify(filename: str, data: bytes, *, max_pages: int = 10) -> str:
    """Pull a representative chunk of text from in-memory bytes for classification.

    Plain text decodes directly. PDFs and DOCXs go through a tempfile so the
    existing path-based extractors can read them.
    """
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".csv"}:
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            log.exception("classify: failed to decode %s", filename)
            return ""
    if suffix in {".pdf", ".docx", ".xlsx"}:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(data)
            tmp_path = Path(tf.name)
        try:
            if suffix == ".pdf":
                text, _ = extract_pdf_text(tmp_path, max_pages=max_pages)
            elif suffix == ".docx":
                from app.services.pdf_extract import extract_docx_text

                text, _ = extract_docx_text(tmp_path)
            else:  # .xlsx
                from app.services.pdf_extract import extract_xlsx_text

                # Cap rows lower for classification — we only need a representative
                # sample to identify the document, not the whole sheet.
                text, _ = extract_xlsx_text(tmp_path, max_rows_per_sheet=80)
            return text
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                log.warning("classify: failed to delete tempfile %s", tmp_path)
    log.info("classify: unsupported file type %s for %s", suffix, filename)
    return ""


def classify_text(*, filename: str, text: str) -> ClassificationResult | None:
    """Run the classifier against already-extracted text. Returns None if the
    LLM call fails or returns an unusable result.
    """
    if not text or len(text.strip()) < 50:
        return None

    settings = get_settings()
    client = get_anthropic()

    try:
        tool_input, usage = client.call_tool(
            model=settings.model_light_extraction,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"FILENAME: {filename}\n\nDOCUMENT TEXT:\n{text[:30000]}"}],
            tool=_TOOL,
            max_tokens=500,
            agent_name="kb_classify",
            proposal_id=None,
        )
    except Exception:
        log.exception("classify: LLM call failed for %s", filename)
        return None

    cls_str = tool_input.get("document_class")
    if not cls_str:
        return None
    try:
        cls_enum = KbDocumentClass(cls_str)
    except ValueError:
        log.warning("classify: model returned invalid class %r", cls_str)
        return None

    log.info(
        "kb_classify: %s -> %s (conf=%s) %s",
        filename,
        cls_str,
        tool_input.get("confidence"),
        fmt_llm_usage(usage),
    )

    raw_tags = tool_input.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = [str(t).strip() for t in raw_tags if str(t).strip()]

    return ClassificationResult(
        document_class=cls_enum,
        confidence=str(tool_input.get("confidence") or "medium"),
        rationale=tool_input.get("rationale"),
        tags=tags,
    )


def classify_kb_upload(filename: str, data: bytes) -> ClassificationResult | None:
    """Classify a staged in-memory file. Extracts text first, then dispatches
    to classify_text."""
    text = _extract_text_for_classify(filename, data)
    return classify_text(filename=filename, text=text)
