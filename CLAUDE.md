# CLAUDE.md

Project-specific context for Claude Code sessions in this repo. Read first; the rest of the codebase makes more sense after this.

## What this is

Multi-agent **proposal factory** for **Quadratic Digital LLC**. Takes an RFP package (PDFs/DOCX) and produces a near-complete proposal package: draft DOCX, compliance matrix, gap analysis, pricing/P&L, cost review. Local-first, single-user. Full design in [`docs/RFP_System_Architecture.md`](docs/RFP_System_Architecture.md) — the source of truth for architecture decisions.

## Run

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
alembic upgrade head        # first time only
python -m app.main           # http://localhost:8000
```

If `Re-classify all` or any agent call fails with "ANTHROPIC_API_KEY not set" while the key IS in `.env`: PowerShell may have an empty env var overriding the file. Clear it:
```powershell
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
```
Pydantic-settings prioritizes process env over `.env`.

## Where things live

- `app/agents/` — LLM-backed agents. One file per agent role.
- `app/jobs/` — background pipelines that orchestrate agents (intake, KB ingestion, reclassify).
- `app/services/` — sync helpers shared by UI + jobs (LLM client, PDF/DOCX extraction, KB / proposal CRUD, profile-suggestion apply logic).
- `app/ui/pages.py` — every NiceGUI page in one module. Long; use `grep` to navigate.
- `app/models/` — SQLAlchemy models, one per domain area. Schema mirrors design doc §10.
- `app/core/enums.py` — every enum referenced across the system. KB document classes live here.
- `data/company_profile.json` — canonical profile, loaded into every agent. Versioned via `_meta.version`.
- `data/kb_documents/{id}/` — uploaded KB files.
- `data/rfp_packages/{id}/` — uploaded RFP files per proposal.

## Hard constraints (non-negotiable)

These come from the design doc and the system depends on them:

1. **No automated submission, ever.** The system drafts and reviews; humans submit. Misrepresentation in a government proposal can result in FAR-based debarment.
2. **Past performance citations only trace to `past_performance_won` / `past_performance_subbed`.** Pending or lost prior proposals can ground voice but cannot be cited as completed work. Reviewer A enforces this when it lands (Weeks 9–10).
3. **Profile suggestions are never auto-applied.** Every change to `data/company_profile.json` requires explicit human approval via the Pending Profile Updates panel.
4. **No competitor proposals, no copyrighted training material.** Even FOIA-released other-firm proposals are excluded. See design doc §15 — copyright + procurement-integrity risk + authenticity issues. Procurement-craft KB is limited to public-domain government guides, free APMP/Shipley abstracts, and Quadratic's own house style notes.
5. **`company_profile.json` is the canonical source of truth.** Don't hardcode company facts in agent prompts; load from the profile loader.

## Conventions in this codebase

- **Background work uses daemon threads** kicked off from UI handlers (`spawn_intake`, `spawn_kb_ingest`, `spawn_reclassify_all`). RQ is in `pyproject.toml` but not wired up; defer that move until we have multi-stage pipelines that need persistence across restarts.
- **SQLite foreign keys are enabled** via a `PRAGMA foreign_keys=ON` connect listener in `app/db/session.py`. Without that, ON DELETE CASCADE silently no-ops on SQLite.
- **`session_scope()` commits on exit, rolls back on exception.** Always release the session before doing LLM calls — accessing ORM attributes after `session_scope` exits raises `DetachedInstanceError`. Read primitives into local vars or dicts inside the `with` block, then call out.
- **NiceGUI 3.x note:** the `app` import collides with our local `app/` package. Use `from nicegui import app as nicegui_app`. Query parameters do NOT auto-bind to function args — read them off `Request` (see `config_page` for the pattern).
- **LLM calls go through `app/services/llm.py`.** It records cost to `agent_runs` (when a `proposal_id` is provided) and uses streaming for tool calls (the SDK refuses non-streaming above ~21K max_tokens).

## Behavioral guidelines (general LLM-coding hygiene)

Adapted from <https://github.com/forrestchang/andrej-karpathy-skills>. These bias toward caution over speed; for trivial tasks, use judgment.

### Think before coding

- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### Simplicity first

- Minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked. No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

### Surgical changes

- Touch only what you must. Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- Remove imports/variables/functions that YOUR changes made unused; don't remove pre-existing dead code unless asked.
- Every changed line should trace directly to the user's request.

### Goal-driven execution

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan with verification steps. Strong success criteria let work proceed independently; weak criteria ("make it work") require constant clarification.
