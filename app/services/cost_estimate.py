"""Rough USD cost estimator for the intake + full pipeline.

Heuristic-based — we don't actually parse the PDFs (that's what the
intake job does), just estimate page count from staged file size and
project from there. Actual costs vary with RFP complexity, number of
compliance items, and how many auto-loop passes each section needs to
converge.

Treat estimates as ±50%. The point is to give the user a sense of
magnitude before they hit Run, not a binding quote.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.services.llm import ANTHROPIC_PRICES, GEMINI_PRICES, OPENAI_PRICES

# Heuristics tuned against the CRM RFI test bed (~98 compliance items,
# 18 outline sections, ~8 PDF pages).
_KB_PER_PAGE = 30  # PDF compression: ~30 KB / page
_TOKENS_PER_PAGE = 750  # ~3000 chars/page × ~0.25 tokens/char
_DEFAULT_REQ_PER_PAGE = 12  # item density typical for govt RFPs
_DEFAULT_SECTIONS = 18  # typical outline section count
_AVG_AUTO_LOOP_PASSES = 3  # mean per-section pass count
_SHORTFALL_BATCH_SIZE = 25  # items per Shortfall Strategist call
_CACHED_PREFIX_TOKENS = 50_000  # ballpark cached prefix (profile + KB + …)


def _price(model: str) -> tuple[float, float]:
    """Return (input_per_mtok, output_per_mtok) for `model`. Falls back
    to Sonnet pricing if the model name isn't in any provider table."""
    if model.startswith("claude-"):
        return ANTHROPIC_PRICES.get(model, (3.0, 15.0))
    if model.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return OPENAI_PRICES.get(model, (3.0, 15.0))
    if model.startswith("gemini-"):
        return GEMINI_PRICES.get(model, (0.30, 2.50))
    return (3.0, 15.0)


def _cost(
    model: str,
    in_tok: int,
    out_tok: int,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Cache-aware cost computation. Anthropic-style multipliers
    (cache_write × 1.25, cache_read × 0.10) are applied. OpenAI's
    auto-cache uses a 0.50× read multiplier; we approximate that here
    by passing the cache_read amount and accepting a slight over-
    estimate (treats OpenAI cache as cheap as Anthropic's). Gemini has
    no transparent cache — pass cache_read=0 for those."""
    in_price, out_price = _price(model)
    base_in = max(in_tok - cache_read - cache_write, 0)
    return (
        base_in * in_price
        + cache_write * in_price * 1.25
        + cache_read * in_price * 0.10
        + out_tok * out_price
    ) / 1_000_000


@dataclass
class CostEstimate:
    """USD breakdown by pipeline phase. All values are point estimates
    with a generous tolerance (±50%) — see the constants above for the
    underlying heuristics."""

    # Intake phase (runs immediately on Run-click)
    intake_metadata: float
    compliance_matrix: float
    shortfall: float
    intake_total: float
    # Post-intake phases (user-driven, run later)
    outline: float
    writer_initial: float
    reviewer_loop: float
    writer_revisions: float
    pipeline_total: float
    # Sizing assumptions surfaced for UI display
    pages_estimated: int
    tokens_estimated: int
    requirements_estimated: int
    sections_estimated: int


def estimate_pipeline_cost(staged_files: dict[str, bytes]) -> CostEstimate:
    """Heuristic dollar estimate for the full pipeline against this
    set of staged RFP files. Returns a `CostEstimate` with per-phase
    breakdown — the UI dialog renders this directly."""
    settings = get_settings()

    total_bytes = sum(len(d) for d in staged_files.values()) or 1
    pages = max(1, total_bytes // (_KB_PER_PAGE * 1024))
    rfp_tokens = pages * _TOKENS_PER_PAGE
    n_reqs = max(20, pages * _DEFAULT_REQ_PER_PAGE)
    n_sections = _DEFAULT_SECTIONS

    # ---------- Intake (runs on Run-click) ----------

    # Metadata extraction — Haiku, full RFP in, small structured out.
    intake_metadata = _cost(
        settings.model_light_extraction,
        rfp_tokens,
        800,
    )

    # Compliance Matrix Agent — Sonnet/drafter; full RFP in, ~80 tokens
    # of output per requirement extracted.
    cm_out = max(2_000, n_reqs * 80)
    compliance_matrix = _cost(settings.model_drafter, rfp_tokens, cm_out)

    # Shortfall Strategist — Sonnet, batched at _SHORTFALL_BATCH_SIZE.
    # First batch writes the cache; subsequent batches read it.
    n_batches = max(1, (n_reqs + _SHORTFALL_BATCH_SIZE - 1) // _SHORTFALL_BATCH_SIZE)
    per_batch_new_in = 5_000
    per_batch_out = 12_000
    sf_first = _cost(
        settings.model_drafter,
        _CACHED_PREFIX_TOKENS + per_batch_new_in,
        per_batch_out,
        cache_write=_CACHED_PREFIX_TOKENS,
    )
    sf_rest = (n_batches - 1) * _cost(
        settings.model_drafter,
        _CACHED_PREFIX_TOKENS + per_batch_new_in,
        per_batch_out,
        cache_read=_CACHED_PREFIX_TOKENS,
    )
    shortfall = sf_first + sf_rest

    intake_total = intake_metadata + compliance_matrix + shortfall

    # ---------- Post-intake (user-driven, run later) ----------

    # Outline Agent — drafter; reads the same cached context plus RFP.
    outline_cost = _cost(settings.model_drafter, _CACHED_PREFIX_TOKENS + rfp_tokens, 5_000)

    # Writer Team — initial draft. Sonnet (cheaper); 18 sections, ~80K
    # cached prefix shared across them.
    writer_cached = 80_000
    writer_init_first = _cost(
        settings.model_writer_team_initial,
        writer_cached + 3_000,
        8_000,
        cache_write=writer_cached,
    )
    writer_init_rest = (n_sections - 1) * _cost(
        settings.model_writer_team_initial,
        writer_cached + 3_000,
        8_000,
        cache_read=writer_cached,
    )
    writer_initial = writer_init_first + writer_init_rest

    # Auto-loop reviewers per pass: Reviewer A (OpenAI/GPT-5.5) + B (Gemini Flash).
    rev_a_per = _cost(
        settings.model_reviewer_a,
        60_000 + 3_000,
        1_500,
        cache_read=50_000,  # OpenAI auto-cache; ~10% multiplier overstates savings vs ~50%
    )
    rev_b_per = _cost(settings.model_reviewer_b, 50_000 + 3_000, 1_500)
    reviewer_loop = n_sections * _AVG_AUTO_LOOP_PASSES * (rev_a_per + rev_b_per)

    # Writer revisions — pass-bracketed schedule (Sonnet pass 1-2,
    # GPT-5.5 pass 3-4, Opus pass 5-6). Roughly (_AVG_AUTO_LOOP_PASSES - 1)
    # revisions per section. With _AVG_AUTO_LOOP_PASSES=3 the average
    # section's 2 revisions both fall in the Sonnet bracket; sections that
    # blow through to passes 3-4 pay more, but those are the minority.
    # Estimate uses the pass_1_2 (Sonnet) tier as the typical cost; the
    # tail of stuck sections that escalate is an under-count, accepted as
    # a conservative bias toward the user.
    writer_rev_per = _cost(
        settings.model_writer_team_pass_1_2,
        writer_cached + 3_500,
        8_000,
        cache_read=writer_cached,
    )
    writer_revisions = n_sections * (_AVG_AUTO_LOOP_PASSES - 1) * writer_rev_per

    pipeline_total = intake_total + outline_cost + writer_initial + reviewer_loop + writer_revisions

    return CostEstimate(
        intake_metadata=intake_metadata,
        compliance_matrix=compliance_matrix,
        shortfall=shortfall,
        intake_total=intake_total,
        outline=outline_cost,
        writer_initial=writer_initial,
        reviewer_loop=reviewer_loop,
        writer_revisions=writer_revisions,
        pipeline_total=pipeline_total,
        pages_estimated=pages,
        tokens_estimated=rfp_tokens,
        requirements_estimated=n_reqs,
        sections_estimated=n_sections,
    )


__all__ = ["CostEstimate", "estimate_pipeline_cost"]
