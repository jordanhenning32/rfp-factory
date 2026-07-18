# RFP Factory — System Architecture

**Version:** 2.0
**Date:** 2026-04-29
**Owner:** Jordan Henning
**Stack:** NiceGUI 3.x + FastAPI + SQLite + SQLAlchemy 2.0 + Alembic + Anthropic + OpenAI + Google GenAI + python-docx, Python 3.12+
**Run:** `Remove-Item Env:ANTHROPIC_API_KEY,Env:OPENAI_API_KEY,Env:GEMINI_API_KEY,Env:GROK_API_KEY -ErrorAction SilentlyContinue; .venv\Scripts\activate; python -m app.main`
**URL:** http://localhost:8000

---

## 1. Executive Summary

RFP Factory is a local-first, single-user agentic system that converts a federal RFP package (PDFs/DOCX) into a submission-ready proposal package: compliance matrix, gap analysis, cost build, drafted sections, reviewer findings, polished cross-section consistency, and a downloadable DOCX with a Submission Checklist appendix.

The system is **opinionated about the workflow** — agents cooperate on a fixed pipeline (intake → outline → team → cost → draft → review → polish → submit), with human checkpoints at every gate that requires judgment (scope sign-off, outline approval, team approval, accept/dismiss findings, final review).

The system is **NOT a one-click "make me a proposal" macro.** The user remains in the loop on every consequential decision. Auto-apply behaviors exist (auto-accept findings, auto-resolve placeholders, auto-apply polish edits) but are bounded, reversible, and audit-logged.

---

## 2. Hard Constraints (Non-Negotiable)

These five rules are FAR/legal/governance and override every other consideration. Every agent prompt enforces them; every reviewer pass checks them.

1. **No automated submission.** Humans submit. Auto-submit risks FAR debarment.
2. **Past-performance citations** ONLY from `past_performance_won` / `past_performance_subbed` KB classes. Pending proposals can ground voice and approach but CANNOT be cited as completed work — Reviewer A flags violations.
3. **Profile suggestions never auto-apply.** Explicit human approval required.
4. **No competitor proposals or copyrighted training material in KB.** Even FOIA-released competitor decks are out of bounds.
5. **Canonical data files** are the single source of truth:
   - `data/company_profile.json` — company facts, key personnel, certifications, capabilities
   - `data/internal_pricing_rules.json` — wrap rates, indirect math, labor catalog, profit policy
   - `data/decisions.json` — cross-RFP institutional memory
   - Never hardcode these in prompts.

---

## 3. Pipeline / Status Flow

The proposal moves through a strict status enum. Forward-only transitions enforced at the service layer; re-running a job after the status has progressed is a safe no-op for the status field.

```
┌─────────────┐
│  INTAKING   │  PDF parse, COTS detection, compliance matrix (Sonnet),
│             │  validator (Gemini), shortfall (Sonnet 6×), auto-mitigations
└──────┬──────┘
       ▼
┌──────────────────────────┐
│ AWAITING_SCOPE_SIGNOFF   │  Human reviews 41-65 gaps, picks mitigations,
│                          │  optional teaming research, signs off scope
└──────┬───────────────────┘
       ▼
┌─────────────┐
│  DRAFTING   │  Outline Agent runs (Sonnet) — proposes section structure
└──────┬──────┘
       ▼
┌────────────────────────────┐
│ AWAITING_OUTLINE_APPROVAL  │  Human reviews outline, can regenerate or approve
└──────┬─────────────────────┘
       ▼
┌────────────────────────────┐
│ AWAITING_TEAM_APPROVAL     │  Human builds team (Team Composer optional),
│                            │  assigns people, approves roster
└──────┬─────────────────────┘
       ▼
┌────────────────────────────┐
│ AWAITING_COST_BUILD        │  Cost Market Researcher (dual: Gemini + Claude+web),
│                            │  Cost Analyst (roster-driven), Cost Reviewer (dual)
└──────┬─────────────────────┘
       ▼
┌────────────────────────────┐
│ AWAITING_DRAFT             │  Two stages on this status:
│                            │  1) Cost Volume Writer drafts SEC-009
│                            │  2) Begin Drafting → Writer Team
└──────┬─────────────────────┘
       ▼
┌────────────────────────────┐
│ DRAFT_IN_PROGRESS          │  Writer Team runs sections in parallel
└──────┬─────────────────────┘
       ▼
┌────────────────────────────┐
│ DRAFT_READY                │  Optional: Auto Review-Revise Loop, Final Polish
└──────┬─────────────────────┘
       ▼
┌────────────────────────────┐
│ REVIEWING                  │  Auto-loop runs Reviewer A + B + Writer Team
└──────┬─────────────────────┘
       ▼
┌─────────────┐
│  APPROVED   │  Human exports DOCX, attaches forms, submits
└──────┬──────┘
       ▼
┌─────────────┐
│  SUBMITTED  │  Awaiting agency response
└─────────────┘
```

Crash recovery on app start: `_BUSY_STATUS_REVERT` reverts `DRAFT_IN_PROGRESS → AWAITING_DRAFT` and similar in-flight statuses, so a killed terminal doesn't leave the proposal stuck.

---

## 4. Agent Roster

Every agent is listed in `app/agents/`. Each has a tool schema, a system prompt, and a thin Python wrapper that records cost to `agent_runs` and handles streaming + retries.

| Agent | Model (default) | Purpose | Lives in |
|---|---|---|---|
| `intake_metadata` | Haiku 4.5 | Extract title, agency, NAICS, due date from RFP | `app/agents/intake_metadata.py` |
| `compliance_matrix` | Sonnet 4.6 | VERBATIM extract every shall/must/should/submission_format requirement | `app/agents/compliance_matrix.py` |
| `compliance_validator` | Gemini 2.5 Pro | Cross-provider validation of compliance matrix output | `app/agents/compliance_validator.py` |
| `kb_classify` | Haiku 4.5 | Classify uploaded KB documents into evidence classes | `app/agents/kb_classify.py` |
| `kb_facts` | Haiku 4.5 | Extract structured facts from KB docs | `app/agents/kb_facts.py` |
| `shortfall_strategist` | Sonnet 4.6 | Per-gap mitigation analysis (teaming / self-perform / custom-build / equiv-experience / no-bid) | `app/agents/shortfall_strategist.py` |
| `consistency_checker` | Haiku 4.5 | Cross-doc consistency on intake metadata | `app/agents/consistency_checker.py` |
| `teaming_researcher` | Gemini 2.5 Pro grounded | Pass A: Google Search-grounded partner research per gap | `app/agents/teaming_researcher.py` |
| `teaming_researcher_claude` | Sonnet 4.6 + web_search | Pass B: Claude+Brave-search partner research per gap | `app/agents/teaming_researcher_claude.py` |
| `teaming_consolidator` | (pure Python) | Union Pass A + Pass B partner suggestions, boost confidence on consensus | `app/agents/teaming_consolidator.py` |
| `market_researcher` | Gemini 2.5 Pro grounded | Pass A: comparable awards + competitor rates | `app/agents/market_researcher.py` |
| `market_researcher_claude` | Sonnet 4.6 + web_search | Pass B: same scope, different search backend | `app/agents/market_researcher_claude.py` |
| `market_consolidator` | (pure Python) | Union awards + competitors with provenance | `app/agents/market_consolidator.py` |
| `outline_agent` | Sonnet 4.6 | Propose section structure | `app/agents/outline_agent.py` |
| `team_composer` | Sonnet 4.6 | Propose 4-8 roles with rationale | `app/agents/team_composer.py` |
| `cost_analyst` | Sonnet 4.6 | Build H/M/L scenarios; consumes approved roster verbatim | `app/agents/cost_analyst.py` |
| `cost_reviewer` | Sonnet 4.6 ×2 | Dual-reviewer adversarial check on cost build | `app/agents/cost_reviewer.py` |
| `cost_review_consolidator` | Sonnet 4.6 | Merge two reviewer outputs into consensus + minorities | `app/agents/cost_review_consolidator.py` |
| `cost_review_strategy` | Sonnet 4.6 | Synthesize findings into a value-first response strategy | `app/agents/cost_review_strategy.py` |
| `cost_review_refiner` | Sonnet 4.6 | "Refine with AI" interactive refinement of strategy | `app/agents/cost_review_refiner.py` |
| `strategy_implementer` | Sonnet 4.6 | Translate cost-review strategy into per-section USER DIRECTIVE strings | `app/agents/strategy_implementer.py` |
| `cost_writer` | Sonnet 4.6 | Draft cost-deferred sections (SEC-009 etc.) using COST_BUILD verbatim | `app/agents/cost_writer.py` |
| `writer_team` | Sonnet 4.6 (initial) / Opus pass-bracketed (revision) | Draft narrative sections in parallel | `app/agents/writer_team.py` |
| `reviewer_a` | Opus 4.7 | Compliance + risk reviewer | `app/agents/reviewer_a.py` |
| `reviewer_b` | Gemini 2.5 Pro | Persuasion reviewer | `app/agents/reviewer_b.py` |
| `needs_human_advisor` | Haiku 4.5 | Suggests value for [NEEDS_HUMAN] placeholders | `app/agents/needs_human_advisor.py` |
| `needs_human_resolver` | Sonnet 4.6 | LLM-driven auto-resolver for placeholders | `app/agents/needs_human_resolver.py` |
| `final_polish_detector` | Gemini 2.5 Pro | Cross-section consistency drift detection | `app/agents/final_polish_detector.py` |
| `final_polish_applier` | Sonnet 4.6 | Surgical edits per detector finding | `app/agents/final_polish_applier.py` |

**Why the model split:** Gemini's 2M context window is unbeatable for whole-corpus synthesis (compliance validator, polish detector, market research grounding). But Gemini occasionally returns empty content instead of an empty tool-call array — silent failure for any agent that EDITS prose, so all drafting/editing agents use Sonnet 4.6 (initial drafts) or Opus (revisions, where reasoning quality matters most).

---

## 5. Data Model

Schema lives in `app/models/`. Migrations in `alembic/versions/` (currently at head **0024**).

### Core

- **`Proposal`** — one row per proposal; FK to `RfpPackage`. Tracks status, cots_orientation, team_approved_at, cost_review_strategy cache, framing answers (`teaming_framing`, `build_framing` — added 0021), title, agency, due_date.
- **`RfpPackage` / `RfpPackageDocument`** — uploaded RFP files + extracted text per doc.
- **`ComplianceMatrixItem`** — one row per requirement extracted by the Compliance Matrix Agent. Fields: requirement_id, requirement_text (verbatim), source_doc/section/page, requirement_type (shall/must/should/submission_format/evaluation_criterion/mandatory_form), category, weight, submission_obtained, submission_notes, **excluded_from_outline** (added 0022 — user "Mark N/A" override).
- **`GapAnalysis`** — one row per shortfall finding. mitigation_options_json (list of dicts), recommended_mitigation_index, selected_mitigation_index, selected_partner_name, resolved, resolution_notes.

### Sections + Drafting

- **`ProposalSection`** — Outline Agent output + Writer Team drafts. draft_text_markdown, citations_json, **needs_human_placeholders_json** (canonical schema: `{marker_text, description, category}`), shortfall_mitigations_applied_json, current_revision_number, requires_cost_analysis, excluded_from_draft.
- **`PolishEdit`** — added 0024. One row per Final Polish auto-applied edit. section_id_label, issue_type, severity, edit_summary, rationale, problematic_text, suggested_fix, applied_at, **applied_in_run_at** (groups edits by polish run for the UI).

### Reviewers + Findings

- **`ReviewerFinding`** — per-section finding from Reviewer A or B. severity, category, finding_text, suggested_fix, accepted_at, dismissed_at, dismissed_reason, resolved_in_pass_number.
- **`CostReviewFinding`** — per-pricing-package finding. user_action enum (pending / accepted / rejected). Distinct on finding_text — same logical finding across LOW/MED/HIGH scenarios deduplicates in the UI.

### Pricing + Market

- **`PricingPackage` / `PricingPackageLine`** — cost build output per scenario.
- **`MarketScan` / `MarketScanComparableAward` / `MarketScanCompetitor`** — Cost Market Researcher output. Detail rows have **`confirmed_by JSON` + `needs_review BOOLEAN`** columns (added 0023) for dual-pipeline provenance.

### Team + Submission

- **`ProposalTeamMember`** — added 0018. Roster row per role: role_name, person_kind (named/tbh/sub), assigned_person, labor_category, wage_band, time_allocation_pct, experience_years, bio_summary, phases_active.
- **`SubmissionCommitment`** — user-tracked deliverable artifacts beyond the auto-extracted forms/certs.

### Other

- **`AgentRun`** — every LLM call's cost ledger. agent_name, model, input_tokens, output_tokens, cost_usd, started_at/completed_at, status, error_text. Used for the Spend tab + the Final Polish tab's "Recent runs" panel.
- **`KnowledgeBaseDocument` / `KnowledgeBaseChunk`** — KB ingest pipeline.
- **`ProfileSuggestion`** — pending edits to company_profile.json awaiting human approval.
- **`LearnedRule`** — per-category dismiss-rate history that feeds reviewer-guidance prompts.

---

## 6. UI Structure

Single-page NiceGUI app. Routes:
- `/` — Proposals in flight (home)
- `/proposals/new` — upload RFP + start intake
- `/proposals/{id}` — main working page (tabbed interface)
- `/proposals/{id}/progress` — Run Progress (live stage banner)
- `/kb` — Knowledge base (docs + learned rules)
- `/config` — settings (profile, pricing rules, models, decisions, suggestions)
- `/admin` — model overrides + cost caps

### Tab order on the proposal page (Phase 2B, current)

```
Compliance · Gaps · Outline · Team · Cost · Cost Review ·
Draft · Reviewer Findings · Final Polish · Completed Draft ·
Submission Checklist · Spend
```

Order matches operations sequence. Removed in this session: Audit Trail (placeholder, never wired), Needs Human Input (redundant — per-section action card on Draft tab covers it). Teaming Strategy moved into Gaps as a sub-tab.

### Tab purposes

- **Compliance** — read-only view of every extracted requirement (ID, type, category, source page, weight).
- **Gaps** — sub-tabs: "Per gap" (default — shortfall cards with mitigation pickers + the **Framing panel** for bulk-set + apply-to-unaddressed) and "Teaming partners" (matrix + per-partner cards with **CONSENSUS / Gemini only / Claude only / Verify** chips from the dual researcher).
- **Outline** — section structure with **Approve Outline** button + ✓ chip after approval. Unassigned-items panel has per-row dropdown with **"Mark N/A — not a narrative item"** sentinel. Cost-section chip (renamed from Cost-deferred).
- **Team** — roster CRUD. Role-name and Labor-category fields are searchable suggestion-dropdowns sourced from `key_personnel.role` and `pricing_rules.labor_catalog` + `labor_rate_card.categories`, with `add-unique` so users can type custom values. **Propose Team (AI)** kicks off the Team Composer.
- **Cost** — Cost pipeline status + Market Research / Cost Analyst / Cost Volume Writer / Cost Reviewer launchers. Awards + Competitors tables now have **Source column** (`✓ Both / Gemini / Claude`) + summary chips + provenance from `confirmed_by`.
- **Cost Review** — findings + the value-first Strategy + Strategy Implementer + AUTO-ACCEPTED chips for consensus CRITICAL/MAJOR.
- **Draft** — section cards. Each has Edit / Refine with AI / Regenerate. Header has **"Draft N missing sections"** button (resume mode) + **"Force restart"** for stuck DRAFT_IN_PROGRESS. Active-section tracking prevents the writer-team batch from racing per-section regenerates.
- **Reviewer Findings** — per-section findings grouped by section. **"Show all" toggle** (default OFF — only pending visible). **"Accept all N pending" button** + auto-accept on every reviewer pass (default config). **Next-step banner** with **"Apply all N sections → regenerate"** master button after triage.
- **Final Polish** — Run button + ✓ green chip after completion + recent edits list (grouped by polish run, expanded most-recent) + auto-toast on running→done transition. Each polish edit shows section + severity + issue type + summary + collapsible before/after.
- **Completed Draft** — letter-page-styled inline preview (8.5″ width / 1″ padding / Calibri 11pt / navy headings) + **Download DOCX** + **Copy markdown** + **Refresh**. Toggles for "Include Submission Checklist appendix" + "Include cost-deferred sections". Citation markers stripped at compile time.
- **Submission Checklist** — system-verified readiness checks + RFP-required forms/certs + user-tracked commitments.
- **Spend** — agent_runs cost ledger.

### Tab badges (red-circle-with-number rule)

Only displayed when the user has actions remaining in that tab. Audited to match exactly what the user sees inside.

| Tab | Badge counts |
|---|---|
| Gaps | unaddressed (resolved=False AND selected_mitigation_index IS NULL) |
| Submission Checklist | unobtained mandatory_form/certification matrix items + unobtained commitments |
| Reviewer Findings | pending findings (not accepted, not dismissed, not auto-resolved) |
| Cost Review | distinct(finding_text) where user_action = 'pending' |

Outline + Draft tabs intentionally have no badge — those are agent-work indicators, not user-action queues.

---

## 7. Key Services (`app/services/`)

| Module | Purpose |
|---|---|
| `llm.py` | Single source for ALL LLM calls. `AnthropicSync.call_tool / complete / complete_with_web_search`, `GeminiSync.call_tool / complete_with_search`, `OpenAISync.call_tool / complete`. Records every call to `agent_runs`. Streams tool calls (SDK refuses non-streaming above ~21K max_tokens). Wraps every call in `_retry_on_transient_error` (4xx + 5xx 502/503/504, deliberately NOT 500). |
| `sections.py` | ProposalSection persistence helpers. `replace_outline`, `persist_section_draft` (revision++), `clear_section_draft`, `assign_compliance_item_to_section`, `mark_compliance_item_outline_excluded`, `compile_proposal_markdown` (cite-stripped, with skipped/included metadata), `strip_citation_markers`. |
| `team.py` | Roster CRUD + the formatter that builds the APPROVED TEAM ROSTER block in the Writer cached prefix. `roster_to_labor_lines` (deterministic FTE→hours×wage_band conversion), `format_team_block_for_writer`, `format_team_roster_for_cost_analyst`, `list_role_names`, `list_labor_categories`, `_default_wage_band_for_category` (catalog fallback when user leaves Salary blank). |
| `pricing.py` | Pricing rules accessor (`get_pricing_rules`), `format_cost_build_block_for_writer`, scenario math, `_parse_wage_band` validator. |
| `framing.py` | The two strategic posture answers + bulk-apply. `set_framing`, `get_framing`, `pick_mitigation_for_framing` (pure ranker), `apply_framing_to_unaddressed_gaps`, `format_framing_block_for_writer` (the third APPROVED FRAMING block in the writer cached prefix). |
| `findings.py` | ReviewerFinding CRUD. `accept_finding`, `dismiss_finding`, `unmark_finding`, `bulk_accept_pending_findings(severity_floor=None)` — runs after every reviewer pass per `auto_accept_findings_severity_floor` config. |
| `polish.py` | `record_polish_edit` (audit row per applied edit), `list_recent_polish_edits_grouped` (UI-friendly run-bundles). |
| `cost_reviewer.py` | Dual-pipeline orchestration + `auto_accept_consensus_findings` (CRITICAL/MAJOR consensus auto-accepted post-upsert). |
| `submission_commitments.py` | Commitment CRUD + `compute_system_verified_items` + `get_submission_checklist_snapshot` (canonical aggregator for forms/certs + commitments + system checks). |
| `decision_capture.py` | After Provide-Value applies, scans every section's resolved placeholders; if 2+ resolutions share the value, opens "Save as cross-RFP decision?" follow-up dialog. |
| `needs_human.py` | Placeholder reconciliation + LLM-driven auto-resolver. Three-layer pattern: deterministic auto-resolve (signatures/dates) → carry-forward of prior resolutions across regen → LLM-resolver. **Schema heal pass** (legacy Cost Writer used `marker` instead of `marker_text`) + **TODO marker detection** (skip auto-resolve for `[ALL_CAPS_IDENTIFIER]` style markers). |
| `export.py` | DOCX export. `compile_proposal_to_docx(proposal_id, *, include_submission_checklist=True, include_cost_deferred=True, proposal_title=None)` — markdown→docx walker covering H1-H4, bold/italic/code, bulleted/numbered lists, markdown tables (with alignment row stripped), block quotes. 1″ margins, Calibri 11pt body, navy 16/13/12pt headings. Submission Checklist appendix (page-break-before) with three sub-tables: RFP-required (table) / user commitments (bullets) / system-verified (checkboxed). |
| `cancellation.py` | Job-kind registry + active-section tracking. `add_active_section`/`remove_active_section` keys (proposal_id, section_pk). Both writer team batch AND per-section regen use it now (race fix from this session). |
| `kb.py` / `kb_context.py` | KB ingest, classification, retrieval helpers for the cached prefix. |
| `market_scan.py` | MarketScan persistence. `upsert_market_scan` (replaces on re-run), `get_market_scan_snapshot`. Reads `confirmed_by` + `needs_review` for the UI chips. |
| `stages.py` | `record_stage(proposal_id, message)` — UI live status banner. Read on the Run Progress page. |

---

## 8. Configuration (`app/config.py`)

Pydantic Settings, loaded once at process startup. **Restart required to pick up changes to `.env` or `app/config.py`.**

Key knobs:

```python
# Compliance + validation
model_compliance_matrix: str = "claude-sonnet-4-6"
model_compliance_validator: str = "gemini-2.5-pro"

# Drafting
model_drafter: str = "claude-sonnet-4-6"
model_writer_team_initial: str = "claude-sonnet-4-6"
# Revision model is pass-bracketed: model_writer_team_for_pass(N)
model_cost_writer: str = "claude-sonnet-4-6"

# Reviewers
model_reviewer_a: str = "claude-opus-4-7"
model_reviewer_b: str = "gemini-2.5-pro"

# Cost
model_cost_analyst: str = "claude-sonnet-4-6"
model_cost_reviewer_primary: str = "claude-sonnet-4-6"
model_cost_reviewer_secondary: str = "claude-sonnet-4-6"
model_cost_review_strategy: str = "claude-sonnet-4-6"

# Market research dual pipeline
model_market_researcher: str = "gemini-2.5-pro"
model_market_researcher_b: str = "claude-sonnet-4-6"
model_teaming_researcher: str = "gemini-2.5-pro"
model_teaming_researcher_b: str = "claude-sonnet-4-6"
model_teaming_consolidator: str = "claude-haiku-4-5-20251001"

# Final Polish
model_polish_detector: str = "gemini-2.5-pro"
model_polish_applier: str = "claude-sonnet-4-6"

# Auto-accept
auto_accept_findings_severity_floor: str | None = None  # None = accept ALL pending

# Light extraction (10+ secondary agents)
model_light_extraction: str = "claude-haiku-4-5-20251001"

# Concurrency
writer_workers: int = 4
shortfall_workers: int = 6  # also reused for compliance matrix per-doc parallelism
```

---

## 9. Engineering Invariants

These are cross-session contracts that won't change with refactors. New code must respect them.

1. **All LLM calls route through `app/services/llm.py`.** Records cost to `agent_runs`. Never instantiate Anthropic / Gemini / OpenAI clients directly outside this module.
2. **`session_scope()` commits on exit, rolls back on exception.** Always release the session BEFORE LLM calls — touching ORM attributes after exit raises `DetachedInstanceError`.
3. **SQLite foreign keys require `PRAGMA foreign_keys=ON`** in `app/db/session.py`. Without it, `ON DELETE CASCADE` silently no-ops.
4. **NiceGUI 3.x:** `from nicegui import app as nicegui_app` — bare `app` collides with our package. Query params do NOT auto-bind to function args.
5. **Background work uses daemon threads.** RQ is in `pyproject.toml` but not wired up — don't assume it's available.
6. **Pydantic Settings loaded at process startup.** Never change `.env` or `app/config.py` defaults mid-run expecting in-flight processes to pick them up — restart between config changes.
7. **`get_company_profile` and `get_pricing_rules` are `@lru_cache`'d.** Edits to JSON files require restart OR explicit `reload_company_profile()` / cache reset.

---

## 10. Established Patterns (Reuse These)

When adding a similar feature, follow the existing pattern:

- **Async LLM calls in dialogs:** always `async def` + `await asyncio.to_thread(sync_fn, ...)`. Sync LLM calls ≥5s freeze the NiceGUI websocket and trigger "Connection lost". Pattern at `pages.py:7316`.
- **CAS-style fire-once gates for sticky toasts:** `claim_<feature>_notification(proposal_id, completed_at)` returns `True` only for the first caller. Needed because `@ui.refreshable` rebuilds register fresh timers each time — without CAS, the same completion event fires the toast N times.
- **Forward-only status transitions:** service-layer status flips check current state and only advance — never rewind. Re-running a job after status has progressed should be a safe no-op for the status field.
- **Three-layer placeholder reduction:** deterministic rules (signatures, dates) → carry-forward of resolved placeholders → LLM resolver. Cheap layers run first; the LLM only sees genuinely ambiguous cases.
- **Cached-prefix injection for new writer context:** add a `format_<thing>_for_writer` formatter that returns empty when the gate isn't yet passed, bracket non-empty output with blank lines, slot into `_CACHED_PREFIX_TEMPLATE` between existing sections. Currently four slots: Team Roster · Cost Build · Framing · (new ones go here).
- **Page-aligned doc splitting:** the PDF parser emits `--- Page N ---` markers; any chunker must split on those, greedy-pack pages to target size, NEVER bisect a page (`source_page` extraction in the next chunk breaks otherwise). Cross-chunk dedupe by `re.sub(r"\s+", " ", text.strip().lower())`, then renumber IDs in document order after merge.
- **Tool-use array recovery for streaming Sonnet:** when `tool_input["items"]` (or any array field) comes back as a `str`, try `json.loads` (handles JSON-encoded array case), then `json_repair.loads` (handles unescaped chars in string values), then split-and-retry as the last resort. See `app/agents/compliance_matrix.py::_parse_items_string`.
- **Dual-provider pipeline (Pass A + Pass B + consolidator):** when high-stakes grounded research is involved, fan two providers in parallel (one per gap or one per scan), consolidate via pure-Python canonicalization. Used by Teaming Researcher, Cost Market Researcher. Cross-provider agreement is itself evidence; single-provider hits get `needs_review: True`.
- **Active-section tracking for race prevention:** every per-section regenerate path calls `add_active_section` / `remove_active_section`. The writer-team batch reads `get_active_sections` and skips those — closes the Apply All vs Force Restart race window.
- **Auto-apply with audit log + reversibility:** auto-accept findings, auto-resolve placeholders, auto-apply polish edits all bump section revisions and write to dedicated audit tables (`polish_edits`) or fields (`accepted_at`, `resolved_in_pass_number`). The user can always revert via per-section regenerate.
- **Citation-stripping at compile time, not at write time:** the writer agents emit `[^cite-N]` markers as breadcrumbs; `compile_proposal_markdown` strips them before any user-facing rendering. The DB still has the markers so audit / regen workflows can reference them.

---

## 11. Anti-Patterns (Learned The Hard Way)

- **Don't add `stop_sequences` to tool-use LLM calls** — markdown content can match the stop string and corrupt drafts. Tool-use already stops at the tool-call boundary.
- **Don't auto-trigger expensive paths** (Gemini grounded, Opus regenerates, teaming research) in pipeline stages — make them opt-in via UI buttons. Auto-fire on every intake bleeds money on no-op cases.
- **Don't truncate at the source when downstream has its own budget.** Per-section retrievers and cached-prefix economics are the real ceilings; source-level char caps just limit coverage on long inputs.
- **Don't auto-apply LLM-suggested category/type changes when the validator only sees a snippet** — the upstream extractor (e.g. Compliance Matrix Agent) has full PDF context the validator doesn't. Defer to upstream when the verb isn't visible in the snippet.
- **Don't trust `alembic autogenerate` to drop indexes** that aren't in the model — they may be performance-helping ones added by an earlier migration. Add `index=True` to the model instead.
- **Don't mock LLMs in e2e tests** without also mocking the side paths that try to instantiate the real client — silent `ANTHROPIC_API_KEY not set` traces from unmocked code paths pollute logs even when tests pass.
- **Don't use Haiku (or any same-family small model) for foundational extraction.** A same-family validator pass shares blind spots with the drafter and becomes near-useless. Compliance Matrix sits at the bottom of the dependency stack — quality matters more than speed there. Sonnet drafter + Gemini validator is the right architecture.
- **Don't put Gemini on a drafter agent** unless empty output is tolerable. Gemini sometimes returns empty content instead of an empty tool-call array (Anthropic always emits the call). For *validators* this is fine ("no findings"); for *drafters* it's a silent failure mode.
- **Don't use array-form types or null enums in Gemini schemas.** `"type": "string"` only — never `["string", "null"]`. No `None` inside `enum` lists. Optionality goes via omission from `required`, not nullability. Bites every new Gemini agent on first run.
- **Don't collapse profile entries that look like duplicate names without verifying.** Two staff entries that differ only by a middle initial (e.g. "First Last" vs "First M. Last") can be *different real people* with different roles, not data errors. The Team service handles this via `(first-initial, last-name)` signature dedupe + a secondary "Profile role" picker.
- **Don't trust Anthropic streaming tool-use to deliver structurally-valid JSON in array fields.** When the model emits verbatim string values containing unescaped `"` chars (common in legal/contract RFPs), the SDK's partial-JSON parser leaves the un-parseable subtree as a raw string in `block.input`. Always check `isinstance(field, list)` on tool-input array fields and have a `json_repair` fallback.
- **Don't mutate JSON-column dicts in place expecting SQLAlchemy to flag dirty.** Always `sec.field = list(updated_copies)`. In-place mutation of dicts inside the column doesn't trigger the dirty-tracker → write doesn't commit.

---

## 12. Operational Runbook

### Starting the app

```powershell
Remove-Item Env:ANTHROPIC_API_KEY,Env:OPENAI_API_KEY,Env:GEMINI_API_KEY,Env:GROK_API_KEY -ErrorAction SilentlyContinue
.venv\Scripts\activate
python -m app.main
```

The `Remove-Item` is required because Pydantic-Settings prioritizes process env over `.env`.

### When to restart

- After any code change (Python doesn't auto-reload modules).
- After any `.env` or `app/config.py` change (Pydantic Settings is loaded once).
- After any `data/company_profile.json` or `data/internal_pricing_rules.json` edit (`@lru_cache`).
- After agent tool-schema changes (the running app caches the schema in agent module imports).

### When to migrate

- After pulling new code that includes a new `alembic/versions/` file.
- `python -m alembic upgrade head` applies pending migrations.
- `python -m alembic check` reports drift between models and DB. Should always be clean.

### Common commands

| Action | Command |
|---|---|
| Migrate to head | `python -m alembic upgrade head` |
| Check drift | `python -m alembic check` |
| Run e2e | `.venv\Scripts\python.exe scripts\_e2e_<name>.py` |
| Syntax check a file | `python -c "import ast; ast.parse(open('<file>').read())"` |

### Test pattern

Per Jordan's preference, e2e scripts live in `scripts/_e2e_*.py` and exercise real wiring. Unit-mocked tests are de-prioritized — the system has too many cross-component contracts (cached prefix injection, status flow gates, agent-run cost recording) for mocks to catch the real bugs.

---

## 13. This Session's Architectural Additions

In rough chronological order across 2026-04-28 → 04-29:

1. **`json_repair` fallback for the Compliance Matrix's streaming tool-use** — Anthropic's partial-JSON parser leaves un-parseable subtrees as raw strings on long arrays with unescaped quotes. Three-layer recovery: `json.loads` → `json_repair.loads` → split-and-retry. Validates the doc-splitter (3-chunk parallel extraction at ~60K chars each).
2. **Teaming Strategy tab moved into Gaps as "Teaming partners" sub-tab.** Decision-making on teaming is a gap-resolution choice; it belongs there.
3. **Submission Checklist tab moved to end** of the row.
4. **Cost tabs reordered** to operations sequence: Team → Cost → Cost Review.
5. **Framing system** (`teaming_framing` + `build_framing` columns on `proposals`, migration 0021). Two strategic-posture questions at the top of Gaps tab → bulk-applies to unaddressed gaps + injects an APPROVED FRAMING block into the writer's cached prefix as the third format-for-writer slot.
6. **Cost-section renaming + toggle removal.** "Cost-deferred" UI term replaced with "Cost section". Toggle removed from outline cards; auto-detection by Outline Agent only.
7. **Outline-tab unassigned items: per-row dropdown** with `SEC-### — Title` options + a "Mark N/A — not a narrative item" sentinel that flips a new `excluded_from_outline` flag on the compliance item (migration 0022).
8. **"Outline approved" green chip** on the Outline tab when status is past `awaiting_outline_approval`.
9. **Next-step banner stale-state fix.** `_render_next_step_banner` now re-queries the proposal status on every refresh instead of capturing it at first render — so post-action transitions (Approve Team → AWAITING_COST_BUILD) flip the banner immediately.
10. **In-page tab switcher** for the "Open Cost tab" / "Open Team tab" banner buttons. URL-hash navigation was a no-op on the same page; now uses `tabs.set_value(name)` via a closure-captured handle.
11. **Header buttons hidden after gaps phase.** "Re-run shortfall" + "Run progress" only render when `status_val in ("intaking", "awaiting_scope_signoff")` — they bleed into later phases otherwise.
12. **Dual-pipeline Teaming Researcher.** Pass A: Gemini grounded. Pass B: Claude+web_search (max_uses=2 for cost control). Consolidator unions partner suggestions by canonical firm name; consensus partners get confidence boost. UI: per-gap-row chips (CONSENSUS / Gemini only / Claude only / Verify) + partner-card-level summary chips. Cost ~$0.20/gap.
13. **Dual-pipeline Cost Market Researcher.** Same pattern, applied to comparable awards (canonicalized title) + competitors (canonicalized firm name). Migration 0023 adds `confirmed_by` + `needs_review` columns to detail tables. UI: Source column + summary chips above each table.
14. **Bulk auto-accept reviewer findings.** New `bulk_accept_pending_findings(proposal_id, severity_floor=None)` service. Wired into `run_reviewer_loop` + `run_reviewer_for_section` per `auto_accept_findings_severity_floor` config (default None = accept all). Plus a UI **"Accept all N pending"** button + filter to show only pending by default.
15. **Apply-all-sections master button** on Reviewer Findings tab — fires per-section regenerate in parallel for every section with accepted findings. Plus a guidance banner explaining the apply→regenerate workflow.
16. **Active-section race fix.** Per-section regenerate now `add_active_section` / `remove_active_section` so the writer-team batch can see in-flight sections and skip them. Writer team batch also queries accepted findings per section and uses them as a directive (not just empty section_brief drafts).
17. **Cost Writer schema fix.** Tool schema migrated from `marker` → `marker_text` + added `category` enum to match Writer Team's. Plus `_normalize_placeholder_schema` heal pass in reconcile so legacy zombie placeholders become actionable + `_is_todo_marker` detection so `[ALL_CAPS_IDENTIFIER]` markers don't auto-resolve.
18. **Option-year / multi-year pricing defaults.** `default_annual_labor_escalation_rate: 0.03` in `internal_pricing_rules.json` + Cost Writer system prompt rewritten with a "REQUIREMENT-DRIVEN COST CONTENT YOU MUST PROVIDE" section that closes off four common deferrals (option-year, Attachment D form replicas, fee absorption, optional services).
19. **Address canonicalization.** `company.headquarters.registered_address` corrected so registered and physical office render identically. New `_usage_notes_for_agents.address_canonical` directive.
20. **Wage band fallback for TBH.** `_default_wage_band_for_category` reads `default_wage_band` from `pricing_rules.labor_catalog` so empty user-typed Salary fields don't crash the Cost Analyst.
21. **Searchable+editable Role / Labor-category dropdowns** on the Edit Team Member dialog. `list_role_names` / `list_labor_categories` helpers. `with_input + new_value_mode='add-unique'` lets users type custom values.
22. **Final Polish Pass agent.** Two-stage: Gemini detector (cross-section drift surface) + Sonnet applier (surgical per-issue edits). Pure-Python orchestrator with active-section tracking. Migration 0024 adds `polish_edits` audit table. UI: own tab with Run button, ✓ completion chip, recent edits grouped by run with before/after, completion toast on running→done transition.
23. **DOCX export.** `compile_proposal_to_docx` in `app/services/export.py`. Markdown→docx walker covering the full subset our agents emit. Submission Checklist appendix with 3 sub-sections (RFP-required forms/certs as a table, user commitments, system-verified readiness). Citation markers stripped at compile time so `[^cite-N]` never reaches the DOCX.
24. **Letter-page-styled preview** (modal + inline tab). 8.5″ width / 1″ inner padding / Calibri 11pt / navy headings. Matches the DOCX layout so on-screen review is faithful to printed deliverable.
25. **Completed Draft tab** — full inline preview + Download DOCX + Copy markdown + Refresh + toggles for "Include Submission Checklist appendix" / "Include cost-deferred sections".
26. **Tab cleanup.** Audit Trail removed (placeholder, never wired). Needs Human Input removed (redundant with per-section action card on Draft tab + system_verified_items on Submission Checklist).
27. **"Proposals in flight" home-page click** now lands on `/proposals/{id}` (working page) instead of `/proposals/{id}/progress` (run progress) — bypasses the unnecessary Run Progress detour.
28. **Findings tab badges aligned to "user actions remaining" rule.** Outline + Draft badges removed (those count agent work, not user actions). Findings tab shows only pending + accepted by default; "Show all" toggle reveals dismissed/resolved.

---

## 14. Open Items / Tech Debt

- **Misleading log warning on empty Gemini function_call.** `app/services/llm.py` says "switch MODEL_REVIEWER_B to gemini-2.5-pro" — written for a different context, fires unhelpfully when the validator hits Gemini's quirk of not making a tool call when there's nothing to flag. Worth a context-aware copy fix. Not a bug.
- **Shortfall straggler latency.** On the test run, 5 of 6 batches finished within 3min, but batch 4 took until ~5min (extra 2min tail). Common with parallel LLM calls; one batch hits a cold cache or slower API path.
- **`pop_months` extraction.** `app/jobs/cost_writer.py` hardcodes `pop_months = 12` as the orchestrator default. Real RFPs have variable PoP (3-year base + 2 option years, etc.). Cost Writer's prompt now handles option-year guidance from RFP context (section_brief, compliance text), but a real `pop_months` extractor at intake would be cleaner.
- **`model_light_extraction` is Haiku 4.5 globally.** If quality concerns arise on the 10+ secondary agents using it (consistency_checker, teaming_researcher structuring, market_researcher structuring, kb_facts, kb_classify, intake_metadata, needs_human_advisor, lessons, cost_estimate), per-agent overrides similar to the validator pattern make sense.
- **Compliance Matrix doc-splitter recovery depth=1.** If a halved chunk also fails malformed JSON, we drop. Could go deeper (depth=2 → quarters) but evidence is that the issue is content-pattern-specific not size-specific, so further halving doesn't help.
- **Citation source data persistence.** `[^cite-N]` markers strip at compile time but the citation source list lives in `ProposalSection.citations_json`. There's currently no UI surface to show the citation list — could be a "References" appendix in the DOCX (ordered list of source URLs from `citations_json` per section).
- **Final Polish doesn't auto-run after the Auto Review-Revise Loop.** Manual trigger only. Could be auto-triggered post-loop for full-automation flows.
- **No KB-citation consistency check.** Reviewer A flags uncited claims but doesn't cross-check that the cited `citations_json` URL actually contains the claim. Citation-check service exists (`citation_check.py`) but isn't wired into the polish pipeline.

---

## 15. File Map

```
rfp-factory/
├── app/
│   ├── agents/                 # 30+ LLM agent modules
│   ├── core/                   # company_profile loader, enums
│   ├── db/                     # session, base
│   ├── jobs/                   # background-thread orchestrators
│   │   ├── intake.py           # PDF parse → compliance → shortfall
│   │   ├── outline.py
│   │   ├── writer.py           # writer team batch + per-section regen
│   │   ├── cost_writer.py      # cost-deferred sections
│   │   ├── cost_analyst.py
│   │   ├── cost_reviewer.py
│   │   ├── market_researcher.py
│   │   ├── reviewer.py         # auto review-revise loop
│   │   ├── final_polish.py     # NEW
│   │   ├── kb_ingest.py
│   │   └── ...
│   ├── models/                 # SQLAlchemy ORM
│   ├── services/               # 20+ helper modules — see §7
│   ├── ui/
│   │   └── pages.py            # ~14k lines, every NiceGUI page + tab
│   ├── config.py               # Pydantic Settings
│   └── main.py                 # entry point
├── alembic/
│   └── versions/               # 24 migrations (0001 … 0024)
├── data/
│   ├── company_profile.json
│   ├── internal_pricing_rules.json
│   ├── decisions.json
│   ├── teaming_partners.json
│   ├── kb_documents/
│   ├── rfp_packages/           # uploaded RFP files per proposal
│   ├── outputs/
│   ├── backups/
│   └── sqlite.db
├── docs/
│   ├── HANDOFF.md
│   └── RFP_System_Architecture.md   # this file
├── scripts/                    # _e2e_*.py + maintenance scripts
├── pyproject.toml
└── .venv/
```

---

## 16. Cost Targets (Order-of-Magnitude)

For a typical 200-requirement state-IT RFP with 8-9 sections:

| Stage | Cost |
|---|---|
| Intake (compliance + validator + shortfall) | ~$3-4 |
| Teaming research (dual, ~10-20 gaps) | ~$2-5 |
| Cost market research (dual) | ~$0.25 |
| Cost Analyst + Cost Reviewer (dual) | ~$0.50 |
| Outline + Team Composer | ~$0.10 |
| Writer Team initial draft (8 sections) | ~$8-15 |
| Cost Volume Writer (1-2 sections) | ~$1-2 |
| Auto Review-Revise Loop (Opus + Gemini × 6 passes) | ~$30-100 |
| Final Polish (Gemini detect + Sonnet apply) | ~$0.65-1.65 |
| **Total per proposal** | **~$45-130** |

Most of the spend is in the auto-loop. Skip it for time-pressured runs and rely on initial draft + final polish + manual review to ship at ~$15-25.

---

## 17. Future Work (Discussed, Not Built)

- **One-click "Build Full Proposal" macro.** Chains Cost Analyst → Cost Reviewer → Strategy → Apply Strategy → Writer Team → Reviewer A+B → Cost Volume Writer with optional pause checkpoints.
- **Profile auto-extraction from new resumes.** Sonnet parses uploaded resume, proposes a `key_personnel` entry. Closes the gap where KB-only people show in the Team-tab dropdown but autofill is empty.
- **Pattern-memory rejection rules.** Learn from history that the user always rejects "verify NC E-Procurement registration"-style patterns; auto-tag dismiss-likely findings on next pass.
- **Stream compliance matrix output.** Perceived latency improvement, no wall-clock change.
- **Async/best-effort validator pass.** Runs in background after persistence; doesn't block.
- **References appendix in DOCX.** Pull `citations_json` per section, render as numbered footnote list at end of proposal (not the Submission Checklist appendix — separate).
- **Citation-check pre-flight.** Verify that the cited URL actually contains the claim. `citation_check.py` exists; not wired.
- **Proposal-level revert / history view.** A timeline of every regenerate, every polish edit, every accept/dismiss. Currently scattered across agent_runs + per-row updated_at fields.

---

*End of architecture document. Update on schema changes (`alembic/versions/`), new agents (`app/agents/`), tab restructuring (`app/ui/pages.py`), or pipeline-flow changes (status enum). Increment §1 version.*
