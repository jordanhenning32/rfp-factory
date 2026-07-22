from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_dir(raw_value: str | None = None) -> Path:
    """Resolve the active data workspace independently of the process cwd."""
    configured = raw_value if raw_value is not None else os.getenv("RFP_DATA_DIR")
    if not configured or not configured.strip():
        return PROJECT_ROOT / "data"

    candidate = Path(configured.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


DATA_DIR = _resolve_data_dir()
KB_DIR = DATA_DIR / "kb_documents"
RFP_PACKAGES_DIR = DATA_DIR / "rfp_packages"
OUTPUTS_DIR = DATA_DIR / "outputs"
BACKUPS_DIR = DATA_DIR / "backups"
COMPANY_PROFILE_PATH = DATA_DIR / "company_profile.json"

_INSECURE_STORAGE_SECRETS = {
    "dev-only-change-me",
    "change-me-to-a-random-string",
}


def _database_path_for_sqlite(database_url: str) -> Path | None:
    """Resolve a file-backed SQLite URL without creating the database."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    if url.database == ":memory:":
        return None
    path = Path(url.database).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _require_isolated_database(database_url: str, data_dir: Path) -> None:
    """Fail closed when an isolated file workspace points at another DB.

    ``RFP_DATA_DIR`` is used by tests, demos, and operators specifically to
    isolate a workspace.  Mixing its attachments/profile files with a database
    from the canonical workspace is more dangerous than refusing startup.
    """
    try:
        actual = _database_path_for_sqlite(database_url)
    except Exception as exc:
        raise ValueError(
            "RFP_DATA_DIR requires a valid file-backed SQLite DATABASE_URL"
        ) from exc
    expected = (data_dir / "sqlite.db").resolve()
    if actual is None or os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
        raise ValueError(
            "RFP_DATA_DIR requires DATABASE_URL to point to sqlite.db inside "
            "that same workspace"
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    # Local desktop product: do not expose proposal data to the LAN by
    # default. Deployments that intentionally add authentication/reverse
    # proxying can opt into a broader bind explicitly.
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_storage_secret: str = "dev-only-change-me"

    database_url: str = f"sqlite:///{DATA_DIR / 'sqlite.db'}"
    redis_url: str = "redis://redis:6379/0"

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    gemini_api_key: str = ""  # alias for google_api_key — Google's own SDK accepts either name
    grok_api_key: str = ""
    voyage_api_key: str = ""

    @model_validator(mode="after")
    def _require_storage_secret_for_deployments(self) -> Settings:
        env = (self.app_env or "").strip().lower()
        secret = (self.app_storage_secret or "").strip()
        if env not in {"development", "dev", "local", "test", "testing"} and (
            not secret or secret in _INSECURE_STORAGE_SECRETS
        ):
            raise ValueError(
                "APP_STORAGE_SECRET must be set to a non-default value outside development/test."
            )
        return self

    @model_validator(mode="after")
    def _validate_workspace_isolation(self) -> Settings:
        if os.getenv("RFP_DATA_DIR", "").strip():
            _require_isolated_database(self.database_url, DATA_DIR)
        return self

    @property
    def google_or_gemini_key(self) -> str:
        """Use either env var name. GEMINI_API_KEY wins if both set."""
        return self.gemini_api_key or self.google_api_key

    per_run_cost_cap: float = 200.0
    monthly_cost_cap: float = 2000.0

    # Number of sections processed in parallel during the auto review-revise
    # loop. Each worker drives ONE section through its full pass cycle
    # (Reviewer A → Reviewer B → if findings, Writer regenerate, repeat).
    # 4 is a safe default: ~4-5x speedup vs serial on an 18-section RFP,
    # without consistently tripping per-provider rate limits. The 429
    # retry/backoff in services/llm.py absorbs occasional spikes.
    auto_loop_workers: int = 4

    # Number of Shortfall Strategist batches running in parallel during
    # intake. All batches share the same cached prefix (~58K tokens) —
    # whichever batch lands first writes the cache; the rest read it.
    # 6 cuts wall-time roughly 4-5x on a typical 6-batch RFP. Bumped
    # from 4 to 6 as part of the intake-speedup pass; the LLM service
    # already retries on rate-limit 429s, so wider fan-out is safe
    # for typical proposals. Drop back to 4 if a particular provider
    # starts throttling consistently.
    # Also reused as the cap for Compliance Matrix per-document
    # parallelism in app.jobs.intake._run_compliance_matrix.
    shortfall_workers: int = 6

    # Number of Writer Team sections drafted in parallel during the
    # initial draft pass. Each worker runs draft_section against the
    # same cached prefix, so the first-to-land worker writes the prefix
    # to the Anthropic prompt cache; the rest read it. 4 typically cuts
    # wall time ~3-4x on a 10-section outline. The cache-write race
    # costs ~$0.30-0.50/run extra (vs serial) — accepted tradeoff for
    # the latency win.
    writer_workers: int = 4

    # Auto-accept threshold for ReviewerFinding rows produced by the
    # main reviewer pipeline (Reviewer A + B). After every reviewer
    # run, pending findings at or above this severity floor are
    # auto-accepted on the user's behalf — the user can still dismiss
    # any they disagree with before clicking Apply on the section.
    # Default: None — accept ALL pending findings (CRITICAL + MAJOR +
    # MINOR). The reviewers are reliable enough that the bulk of
    # findings are routine fixes the user would always accept; making
    # them click Accept on each one is busywork. Override to "MAJOR"
    # to leave MINOR pending for manual triage, "CRITICAL" for only
    # CRITICAL, or "" / "off" to disable auto-accept entirely.
    auto_accept_findings_severity_floor: str | None = None

    # Drafting agents that aren't the Writer Team (Compliance Matrix, Outline
    # Agent, Shortfall Strategist). Sonnet is the right cost/quality point —
    # these don't need Opus's adversarial reasoning, just verbatim extraction
    # and structured output.
    model_drafter: str = "claude-sonnet-4-6"
    # Writer Team — REVISION model. Default for manual regenerate
    # (Refine-with-AI button, Draft-tab Regenerate). Auto-loop revisions
    # follow the pass-bracketed schedule below (model_writer_team_pass_*)
    # rather than this default. Opus is the right default for the manual
    # path because the user explicitly asked for a regen and wants the
    # heaviest reasoning.
    model_writer_team: str = "claude-opus-4-7"
    # Writer Team — auto-loop REVISION schedule by pass number.
    # The auto Review-Revise loop runs up to 6 passes per section. Most
    # sections converge in pass 1-2; a minority need pass 3-4 with a
    # different provider's perspective; the rare stuck-section gets Opus
    # at pass 5-6. This schedule cuts revision cost ~3-4× without giving
    # up the heaviest model where it actually matters.
    model_writer_team_pass_1_2: str = "claude-sonnet-4-6"
    model_writer_team_pass_3_4: str = "gpt-5.5"
    model_writer_team_pass_5_6: str = "claude-opus-4-7"
    # Writer Team — INITIAL DRAFT model. Used by run_writer_team when first
    # drafting all sections from the approved outline. Sonnet because:
    # (a) Sonnet first-draft quality is 80-90% of Opus on most sections,
    # (b) the auto Review-Revise loop will polish weak sections via Opus
    # revisions anyway, (c) saves ~5x on the most expensive single batch
    # (18 sections × cache write × full-length output).
    model_writer_team_initial: str = "claude-sonnet-4-6"
    # Reviewer A — adversarial honesty / compliance check. GPT-5.5 by default
    # for provider diversity vs the Writer (Opus / Anthropic). Different
    # training distribution = different blind spots; the second-look catches
    # things Anthropic-on-Anthropic would share. Override to claude-opus-4-7
    # if you want the previous default.
    model_reviewer_a: str = "gpt-5.5"
    # Reviewer B — persuasion / evaluator psychology. Gemini for further
    # provider diversity (writer = Anthropic, reviewer A = OpenAI, reviewer B
    # = Google → three distinct training distributions). Pinned to
    # gemini-2.5-pro because gemini-2.5-flash was unreliable on the
    # structured tool-use output: Flash returned candidates with no
    # function_call payload (logged as "Gemini returned no function_call …
    # treating as empty result"), so Reviewer B silently produced zero
    # findings on every section. Pro is ~3-5x more expensive per call but
    # the calls are small ($0.05-0.15 each) and the alternative is a
    # silently broken reviewer.
    model_reviewer_b: str = "gemini-2.5-pro"
    model_light_extraction: str = "claude-haiku-4-5-20251001"
    # Compliance Matrix Agent — extracts every requirement from the
    # RFP package as a structured tool call. The compliance matrix
    # is the foundation everything else builds on (shortfall, outline,
    # writer coverage, submission checklist), so quality matters more
    # than speed. Default = Sonnet 4.6. The setting is exposed as a
    # knob for quick-eval scans where the user wants intake-as-fast-
    # as-possible — set model_compliance_matrix=claude-haiku-4-5-20251001
    # in .env to swap in. Independent Gemini review, bounded Haiku leaf
    # fallback, and deterministic truncation repair sit downstream as safety
    # nets either way.
    model_compliance_matrix: str = "claude-sonnet-4-6"
    # Compliance Matrix VALIDATOR — runs after the drafter to catch
    # type/category drift. Cross-provider on purpose: Sonnet drafts,
    # Gemini 2.5 Pro validates. Same-family models share blind spots,
    # so the validator must be a different family to add real signal.
    # The validator's HIGH-confidence-only mutation policy means
    # over-flagging is bounded — worst case is more visible warnings,
    # not corrupted data. Cost bump vs Haiku is ~$0.50/intake.
    model_compliance_validator: str = "gemini-2.5-pro"
    # Leaf fallback only. The validator first retries a failed Gemini batch
    # in smaller pieces; Haiku is used only if that bounded recovery still
    # cannot produce a valid structured response. Any fallback use is stored
    # and surfaced as a degraded independent-review outcome because both the
    # extractor and fallback then come from Anthropic.
    model_compliance_validator_fallback: str = "claude-haiku-4-5-20251001"
    # Cost Reviewer — adversarial fact-check of the Cost Analyst's
    # output. Looks for missed scope, unrealistic hours, margin
    # pressure vs market, wage-band misalignment, phase gaps,
    # ceiling violations, and ODC reasonableness. Pro-tier model
    # justified: cost mistakes are unrecoverable post-submit.
    # Initial spec called for gemini-3-pro / 3.5-pro for stronger
    # adversarial reasoning, but as of 2026-04-28 those return 404
    # against Google's v1beta API — same NotFound the Market
    # Researcher hit. Falling back to gemini-2.5-pro (the proven Pro
    # model elsewhere in the stack). Override in .env to gemini-3-pro
    # / gemini-3.5-pro once GA.
    model_cost_reviewer: str = "gemini-2.5-pro"
    # Cost Reviewer (secondary) — second adversarial pass with a
    # different provider for consensus filtering. Findings only
    # persist when BOTH reviewers raised the same underlying issue,
    # which dramatically reduces hallucinated / low-confidence
    # findings at the cost of one extra LLM call per Cost Review
    # run. GPT-5.5 chosen for provider diversity vs the primary
    # Gemini reviewer — different training distributions catch
    # different blind spots.
    model_cost_reviewer_secondary: str = "gpt-5.5"
    # Cost Review Consolidator — takes the two reviewers' finding
    # sets and emits the consensus subset (findings that both
    # reviewers raised about the same underlying issue). Sonnet 4.6
    # for strong text matching + synthesis when the two reviewers
    # phrase the same finding differently.
    model_cost_review_consolidator: str = "claude-sonnet-4-6"
    # Cost Review Strategist — given the full set of consensus
    # findings, produces a single coherent strategic plan that
    # addresses them together (accounting for trade-offs like
    # "increase hours AND reduce margin"). Free-form markdown
    # output. Sonnet 4.6 for strong narrative reasoning.
    model_cost_review_strategist: str = "claude-sonnet-4-6"
    # Cost Review Refiner — small interactive agent the user invokes
    # via the "Refine with AI" button on Cost Review findings. Takes
    # one finding plus user-provided context and rewrites the
    # recommended_change to incorporate that context. Sonnet 4.6 is
    # cheap, has strong instruction-following, and matches the
    # editorial voice of the Writer Team output.
    model_cost_review_refiner: str = "claude-sonnet-4-6"
    # Strategy Implementer — translates the cached cost-review
    # strategy into per-section USER DIRECTIVE strings that the
    # Writer Team consumes via spawn_writer_for_section. One
    # structured Sonnet 4.6 tool call. Cheap (~$0.05-0.15) — the
    # downstream cost is the per-section writer regenerates each
    # directive triggers (~$0.50/section).
    model_strategy_implementer: str = "claude-sonnet-4-6"
    # Team Composer — proposes a delivery team roster (roles + GSA
    # OLM labor categories + time allocations + phase coverage)
    # from RFP scope + outline + compliance matrix. Run from the
    # Team tab's "Propose Team (AI)" button. The user reviews and
    # assigns specific people to each proposed role.
    model_team_composer: str = "claude-sonnet-4-6"
    # Needs Human Resolver — Phase B post-pass. After draft_section
    # persists + the deterministic auto-resolver fills signatures
    # and doc-creation dates, this agent looks at whatever
    # [NEEDS_HUMAN] placeholders remain and tries to resolve them
    # against the cached context (company profile, decisions
    # ledger, approved team roster, approved cost build). Be
    # conservative — when in doubt, skip and let the user decide.
    # Sonnet 4.6 for instruction-following at low cost
    # (~$0.02-0.05/section).
    model_needs_human_resolver: str = "claude-sonnet-4-6"
    # Cost Analyst (Agent 2 of the cost pipeline) — synthesizes the
    # market scan + internal pricing rules + scope context into
    # H/M/L scenario labor estimates. GPT-5.5 chosen for strong
    # synthesis across multi-source structured input (market scan
    # + pricing rules JSON + compliance matrix + draft titles) and
    # provider diversity vs the Anthropic-heavy stack elsewhere.
    # The agent returns LABOR JUDGMENT only (categories / salaries
    # / hours / rationale); deterministic Python applies the wrap
    # rate formula and scenario_definitions to compute every dollar.
    model_cost_analyst: str = "gpt-5.5"
    # Cost Volume Writer (Agent 3 of the cost pipeline) — drafts the
    # cost-deferred narrative sections (Cost Volume, Basis of Estimate,
    # Pricing Narrative) from the Cost Analyst's structured output.
    # Sonnet 4.6 chosen to match the existing initial-draft Writer
    # Team default — same prompt-cache economics, consistent voice
    # across the proposal package, sufficient reasoning quality for
    # cost-narrative writing once numbers are pinned by deterministic
    # math. Manual regenerate (Refine-with-AI) inherits this default.
    model_cost_writer: str = "claude-sonnet-4-6"
    # Teaming Researcher — Gemini Pro for partner market research after the
    # Shortfall Strategist runs. Gemini's broader factual recall on
    # govt-contractor cohorts produces more specific partner candidates
    # than Sonnet repeating the small confirmed library. Pro tier matters
    # here: Flash hallucinates firm names too often.
    model_teaming_researcher: str = "gemini-2.5-pro"
    # Teaming Researcher Pass B — Claude Sonnet 4.6 with the
    # web_search_20250305 tool. Pairs with the Gemini-grounded Pass A
    # for cross-provider partner verification: when both providers
    # surface the same firm, the consolidator boosts confidence and
    # the UI marks it CONSENSUS; when only one surfaces it, it's
    # flagged needs_review. Different search backend (Brave under
    # Anthropic vs Google under Gemini) is the whole point.
    model_teaming_researcher_b: str = "claude-sonnet-4-6"
    # Teaming Consolidator — pure-Python merge, no LLM call by default.
    # Knob exists so an LLM-assisted disambiguation step can be wired
    # in later if name-canonicalization misses too many edge cases.
    model_teaming_consolidator: str = "claude-haiku-4-5-20251001"
    # Cost Market Researcher — Gemini Pro grounded for deep-search
    # comparable-awards + competitor research. Initial spec called for
    # gemini-3.5-pro for stronger multi-source synthesis, but as of
    # 2026-04-27 Google's v1beta API returns 404 for that model
    # ("not found for generateContent" — not yet GA). Falling back to
    # gemini-2.5-pro, which is the proven Pro model in this codebase
    # (already runs Reviewer B and Teaming Researcher with grounded
    # search). Override in .env to gemini-3-pro / gemini-3.5-pro once
    # those reach GA. The structuring follow-up uses
    # model_light_extraction (Haiku) per the existing
    # teaming-researcher pattern.
    model_market_researcher: str = "gemini-2.5-pro"
    # Cost Market Researcher Pass B — Claude Sonnet 4.6 with the
    # web_search_20250305 tool. Same dual-pipeline pattern as the
    # Teaming Researcher: cross-provider verification on comparable
    # awards + competitor identification. The search backend (Brave
    # under Anthropic) differs from Gemini's (Google), so consensus
    # rows are independently corroborated. max_uses pinned to 2 in
    # the agent itself to keep the input-token bill bounded.
    model_market_researcher_b: str = "claude-sonnet-4-6"

    # Final Polish Pass — cross-section consistency cleanup. Two-stage
    # pipeline because each stage wants a different model:
    #   detector — Gemini 2.5 Pro. The 2M-token context window matters
    #     when feeding all 8 drafted sections + the cost narrative as
    #     one corpus. Synthesis across documents is exactly Gemini's
    #     strength and the empty-tool-call quirk is acceptable here
    #     (a no-issues output is interpretable as 'corpus is consistent').
    #   applier — Sonnet 4.6. Per the documented anti-pattern, Gemini
    #     is the wrong model for an agent that EDITS prose (silent
    #     empty-output failure mode). Sonnet's structured output is
    #     reliable for "return the new section markdown".
    model_polish_detector: str = "gemini-2.5-pro"
    model_polish_applier: str = "claude-sonnet-4-6"
    # Hard cap on Gemini grounded queries per market-research run.
    # Defends against runaway research sessions burning budget on
    # repeat queries. Each grounded call costs ~$0.05-0.20; 12 caps a
    # worst-case run at ~$2.40. Typical scan uses 1-3 grounded calls.
    market_research_query_budget: int = 12
    embedding_model: str = "voyage-3-large"

    sentry_dsn: str = ""

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in ("development", "dev", "local")

    @property
    def is_demo(self) -> bool:
        return self.app_env.lower() == "demo"

    def model_writer_team_for_pass(self, pass_num: int | None) -> str:
        """Pick the writer-revision model for a given auto-loop pass.

        pass_num=None means "not from the auto-loop" (manual Regenerate /
        Refine-with-AI), in which case we fall back to the legacy
        `model_writer_team` default. Out-of-range passes (>6) map to the
        last bracket so we never crash if the cap is later raised.
        """
        if pass_num is None:
            return self.model_writer_team
        if pass_num <= 2:
            return self.model_writer_team_pass_1_2
        if pass_num <= 4:
            return self.model_writer_team_pass_3_4
        return self.model_writer_team_pass_5_6


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ensure_data_dirs() -> None:
    """Create data subdirectories on startup. Idempotent."""
    for d in (DATA_DIR, KB_DIR, RFP_PACKAGES_DIR, OUTPUTS_DIR, BACKUPS_DIR):
        d.mkdir(parents=True, exist_ok=True)


__all__ = [
    "PROJECT_ROOT",
    "_resolve_data_dir",
    "DATA_DIR",
    "KB_DIR",
    "RFP_PACKAGES_DIR",
    "OUTPUTS_DIR",
    "BACKUPS_DIR",
    "COMPANY_PROFILE_PATH",
    "Settings",
    "get_settings",
    "ensure_data_dirs",
]
