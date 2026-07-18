# RFP Factory — System Architecture

Single-source architecture reference for the Quadratic Digital RFP Factory. Three diagrams cover the system at three resolutions: **system-overview** (layers + externals), **pipeline-flow** (agents + state transitions), and **schema** (DB relationships). Plus a UI module map.

State of this document: 2026-04-30, post-modularization, post-Phase 2B (payment_systems pipeline live).

---

## 1. System Overview

Top-down view of the runtime: browser → app layers → external LLM APIs + filesystem.

```mermaid
graph TB
    User([User<br/>Chrome --app= window])

    subgraph App["RFP Factory · single-process Python"]
        direction TB

        subgraph Web["Web Layer (NiceGUI 3.x + FastAPI)"]
            Pages["app/ui/pages.py<br/>page handlers + page-frame layout"]
            Tabs["app/ui/tabs/*.py<br/>13 tab modules"]
            APIs["@nicegui_app.get<br/>/api/health"]
        end

        subgraph Jobs["Job Orchestration (daemon threads)"]
            JobMod["app/jobs/*.py<br/>intake · outline · writer · reviewer<br/>cost_analyst · cost_writer · cost_reviewer<br/>market_researcher · payment_market_researcher<br/>payment_cost_reviewer · final_polish<br/>strategy_implementer · team_composer"]
            Cancel["app/services/cancellation.py<br/>cancellation registry +<br/>active_section tracking"]
        end

        subgraph Agents["LLM Agents (one file per role)"]
            AgentMod["app/agents/*.py<br/>compliance_matrix · compliance_validator<br/>shortfall_strategist · outline_agent<br/>writer_team · reviewer_a · reviewer_b<br/>cost_analyst · cost_reviewer · cost_writer<br/>market_researcher · market_researcher_claude<br/>payment_market_researcher · payment_*_claude<br/>final_polish_detector · final_polish_applier<br/>needs_human_advisor · needs_human_resolver<br/>strategy_implementer · team_composer<br/>kb_classify · kb_facts · intake_metadata<br/>consistency_checker"]
        end

        subgraph Services["Domain Services"]
            ServiceMod["app/services/*.py<br/>llm (provider dispatch + fmt_llm_usage)<br/>service_line (registry: it_services / payment_systems)<br/>sections · pricing · findings · framing<br/>needs_human · submission_commitments<br/>cost_dashboard · cost_estimate · cost_reviewer<br/>payment_cost_review · polish · timeline<br/>export · grounding_check · citation_check<br/>kb · kb_context · lessons · stages · proposals<br/>preflight_checks · decision_capture · team<br/>market_scan · rfp_retrieval · profile_suggestions"]
        end

        subgraph Data["Persistence"]
            DB[("SQLite<br/>data/sqlite.db")]
            Models["app/models/*.py<br/>32 SQLAlchemy ORM classes"]
            FS_KB[("data/kb_documents/<br/>uploaded KB files")]
            FS_RFP[("data/rfp_packages/<br/>uploaded RFP files")]
            FS_Out[("data/outputs/<br/>generated DOCX exports")]
            FS_Profile[("data/company_profile.json<br/>data/pricing/*.json<br/>internal_pricing_rules.json<br/>teaming_partners.json")]
        end
    end

    subgraph External["External LLM Providers"]
        Anthropic["Anthropic API<br/>Opus 4.7 · Sonnet 4.6 · Haiku 4.5"]
        Gemini["Google Gemini API<br/>2.5 Pro grounded · 2.5 Flash"]
        OpenAI["OpenAI API<br/>GPT-5.5 (cost reviewer Pass B)"]
    end

    User -.->|HTTPS<br/>WebSocket| Web
    Pages --> Tabs
    Pages --> Services
    Pages -->|spawn_*| Jobs
    Tabs --> Services
    Jobs --> Agents
    Jobs --> Services
    Jobs <--> Cancel
    Agents --> Services
    Services <--> Models
    Models <--> DB
    Services <--> FS_KB
    Services <--> FS_RFP
    Services -->|compile_proposal_to_docx| FS_Out
    Services <--> FS_Profile
    Agents -.->|via app.services.llm| Anthropic
    Agents -.->|via app.services.llm| Gemini
    Agents -.->|via app.services.llm| OpenAI

    classDef ext fill:#FEF3C7,stroke:#92400E
    classDef storage fill:#DBEAFE,stroke:#1E40AF
    classDef user fill:#D1FAE5,stroke:#065F46

    class Anthropic,Gemini,OpenAI ext
    class DB,FS_KB,FS_RFP,FS_Out,FS_Profile storage
    class User user
```

**Key design notes:**
- **Single-process, single-user.** No RQ, no Redis, no Celery. Daemon threads kicked off from UI handlers via `spawn_*` functions in `app/jobs/`.
- **Cancellation registry** is module-level state in `app/services/cancellation.py`. Lets the UI cancel auto-loops mid-pass and lets tabs detect "section X is currently regenerating" via `get_active_sections()`.
- **All LLM calls funnel through** `app/services/llm.py`. Single record point for cost-tracking (writes to `agent_runs` table) and the central `fmt_llm_usage()` helper for log lines.
- **Service-line registry** in `app/services/service_line.py` gates which agents/jobs run. Two registered today: `it_services` (default) and `payment_systems`. Adding a new service line is a registry entry + JSON config files.

---

## 2. Pipeline Flow

End-to-end proposal lifecycle. Statuses on the proposal row drive UI affordances; agents fire on user-clicked transitions or background loops.

```mermaid
flowchart TD
    Start([User uploads RFP<br/>via /proposals/new])

    Intake["INTAKING<br/>━━━━━━━━━━<br/>📄 PDF/DOCX text extraction<br/>🤖 intake_metadata (Haiku)<br/>🤖 compliance_matrix (Sonnet)<br/>🤖 compliance_validator (Haiku)<br/>🤖 kb_classify (Haiku)"]

    Shortfall["AWAITING_SCOPE_SIGNOFF<br/>━━━━━━━━━━<br/>🤖 shortfall_strategist (Opus)<br/>identifies deal-breakers + gaps"]

    Outline["DRAFTING / AWAITING_OUTLINE_APPROVAL<br/>━━━━━━━━━━<br/>🤖 outline_agent (Sonnet)<br/>plus user can add Mandatory<br/>Structure Directives"]

    Team["AWAITING_TEAM_APPROVAL<br/>━━━━━━━━━━<br/>👤 manual roster entry<br/>🤖 team_composer (Sonnet)<br/>optional Propose Team AI"]

    CostBranch{Service line?}

    CostIT["AWAITING_COST_BUILD (it_services)<br/>━━━━━━━━━━<br/>🤖 market_researcher A (Gemini grounded)<br/>🤖 market_researcher_claude B (Claude+web)<br/>🤖 market_consolidator (Python merge)<br/>🤖 cost_analyst (Sonnet)<br/>🤖 cost_reviewer (Gemini Pro + GPT-5.5)<br/>🤖 cost_review_consolidator (Sonnet)<br/>🤖 cost_writer (Sonnet)"]

    CostPay["AWAITING_COST_BUILD (payment_systems)<br/>━━━━━━━━━━<br/>🤖 payment_market_researcher A (Gemini)<br/>🤖 payment_market_researcher_claude B<br/>🤖 payment_market_consolidator<br/>🤖 cost_writer (Sonnet)<br/>🤖 payment_cost_reviewer (Sonnet)"]

    Draft["AWAITING_DRAFT → DRAFT_IN_PROGRESS<br/>━━━━━━━━━━<br/>🤖 writer_team per section (Sonnet)<br/>🤖 needs_human_resolver (Haiku)<br/>auto-resolves placeholders w/<br/>cached context"]

    Reviewer["DRAFT_READY → REVIEWING<br/>━━━━━━━━━━<br/>🤖 reviewer_a (Opus)<br/>🤖 reviewer_b (Gemini)<br/>auto-loop: review → accept critical<br/>→ regenerate → re-review (cap 4)"]

    Polish["READY (post review)<br/>━━━━━━━━━━<br/>🤖 final_polish_detector (Gemini Pro)<br/>🤖 final_polish_applier (Sonnet)<br/>cross-section consistency cleanup"]

    Export["READY<br/>━━━━━━━━━━<br/>📄 compile_proposal_to_docx<br/>🤖 extract_submission_filename (Haiku)<br/>respects RFP-mandated naming"]

    Submit([User submits<br/>outside the system])

    Start --> Intake
    Intake --> Shortfall
    Shortfall --> Outline
    Outline --> Team
    Team --> CostBranch
    CostBranch -->|it_services| CostIT
    CostBranch -->|payment_systems| CostPay
    CostIT --> Draft
    CostPay --> Draft
    Draft --> Reviewer
    Reviewer --> Polish
    Polish --> Export
    Export --> Submit

    classDef agent fill:#E0E7FF,stroke:#3730A3
    classDef branch fill:#FEF3C7,stroke:#92400E
    classDef terminal fill:#D1FAE5,stroke:#065F46

    class Intake,Shortfall,Outline,Team,CostIT,CostPay,Draft,Reviewer,Polish,Export agent
    class CostBranch branch
    class Start,Submit terminal
```

**Notes on the flow:**
- **HARD CONSTRAINT (per CLAUDE.md):** No automated submission — the system drafts and reviews; humans submit. Misrepresentation in a federal proposal can result in FAR-based debarment.
- **Every agent records its run** to `agent_runs` table with token counts + USD cost. Surfaced on the **Spend** tab.
- **Auto Review-Revise loop** at `Reviewer` is a true loop — Reviewer A+B run, accept critical findings, writer regenerates, repeat until clean / 4-pass cap / no progress.
- **Gemini outage resilience:** dual-pipeline market researchers (A+B) tolerate Pass A failure — the orchestrator catches the retry-exhausted exception and consolidates with B-only results, marked in `confirmed_by` provenance.

---

## 3. Database Schema (Core Entities)

Proposal-centric; almost everything else FKs back to it with `ondelete=CASCADE`.

```mermaid
erDiagram
    Proposal ||--o{ ComplianceMatrixItem : "has many"
    Proposal ||--o{ GapAnalysis : "has many"
    Proposal ||--o{ ProposalSection : "has many"
    Proposal ||--o{ PricingPackage : "has 3 (LOW/MED/HIGH) + 1 CUSTOM"
    Proposal ||--o{ AgentRun : "has many"
    Proposal ||--o{ MarketScan : "has 0..1"
    Proposal ||--o{ PolishEdit : "has many"
    Proposal ||--o{ TeamMember : "has many"
    Proposal ||--o{ SubmissionCommitment : "has many"
    Proposal ||--o| RfpPackage : "has 1"

    ComplianceMatrixItem ||--o{ GapAnalysis : "shortfall on"
    ProposalSection ||--o{ ReviewerFinding : "has many"
    ProposalSection ||--o{ PolishEdit : "audited by"
    PricingPackage ||--o{ CostReviewFinding : "reviewed by"
    RfpPackage ||--o{ RfpPackageDocument : "has many"

    KnowledgeBaseDocument ||--o{ KnowledgeBaseChunk : "split into"
    LearnedRule }o--|| ReviewerFinding : "extracted from"

    Proposal {
        int id PK
        string title
        string agency
        date due_date
        enum status "INTAKING / AWAITING_OUTLINE_APPROVAL / DRAFT_IN_PROGRESS / etc"
        string service_line "it_services | payment_systems"
        text payment_market_scan_json
        text payment_cost_review_findings_json
        text timeline_json
        string selected_pricing_model
        string proposed_scenario "LOW | MEDIUM | HIGH | CUSTOM"
    }

    ComplianceMatrixItem {
        int id PK
        int proposal_id FK
        string requirement_id "REQ-001"
        text requirement_text "verbatim"
        enum requirement_type "shall/must/should/submission_format/evaluation_criterion/mandatory_form"
        enum category
        boolean submission_obtained
        boolean excluded_from_outline
    }

    GapAnalysis {
        int id PK
        int proposal_id FK "indexed"
        int requirement_id_fk FK
        enum gap_severity "deal_breaker/major/minor/technical"
        json mitigation_options_json
        int selected_mitigation_index
        string selected_partner_name
        boolean resolved
    }

    ProposalSection {
        int id PK
        int proposal_id FK
        string section_id "SEC-001"
        text draft_text_markdown
        json citations_json
        json needs_human_placeholders_json
        boolean requires_cost_analysis
        boolean excluded_from_draft
        int current_revision_number
    }

    PricingPackage {
        int id PK
        int proposal_id FK
        enum scenario "LOW/MEDIUM/HIGH/CUSTOM"
        json odcs_json
        json indirect_costs_json
        json pnl_projection_json
        json phase_breakdown_json
        enum vs_market_position
        enum bid_recommendation
    }

    ReviewerFinding {
        int id PK
        int proposal_section_id FK
        string reviewer_agent "A | B"
        enum severity "CRITICAL/MAJOR/MINOR"
        enum category
        text finding_text
        text suggested_fix
        datetime accepted_at
        datetime dismissed_at
        int resolved_in_pass_number
    }

    AgentRun {
        int id PK
        int proposal_id FK "indexed"
        string agent_name
        string model_used
        int input_tokens
        int output_tokens
        decimal cost_usd
        enum status "running/completed/failed"
    }

    LearnedRule {
        int id PK
        enum kind "writer_avoid | reviewer_calibrate"
        text rule_text
        enum status "draft | approved | archived"
        int source_finding_id FK
    }

    TeamMember {
        int id PK
        int proposal_id FK
        string role_name
        enum person_kind "named | tbh | sub"
        string assigned_person
        int time_allocation_pct
        json phases_active
    }
```

**Schema notes:**
- **Migrations: 0001 → 0031.** Latest two: `0030` (index `gap_analyses.proposal_id`) and `0031` (`proposals.timeline_json`).
- **Cascade on delete:** dropping a proposal nukes everything FK'd to it. Safe by design — proposals are deleted only via the trash icon, with a confirmation.
- **JSON columns** are used aggressively for "structured data the agents produce" — citations, needs_human_placeholders, phase_breakdown, mitigation_options. Lets the schema absorb agent-output-shape changes without migrations.
- **`agent_runs.proposal_id` is indexed** — every LLM call writes a row; query patterns are always proposal-scoped.

---

## 4. UI Module Map (post-modularization)

After the 14-commit modularization series (`aee9302..1e36e13`), `pages.py` shrunk from 16,381 → 6,822 lines. Each tab lives in its own module under `app/ui/tabs/`.

```mermaid
graph LR
    subgraph "app/ui/"
        Layout["layout.py<br/>page_frame() — header + sidebar"]
        Theme["_theme.py<br/>brand tokens (NAVY, CYAN)<br/>+ asset paths"]
        Shared["_shared.py<br/>_empty_state +<br/>_extract_section_markdown"]
        Pages["pages.py (6,822 lines)<br/>page handlers · home() · proposal_review()<br/>kb · config · admin · run_progress<br/>+ shared dialogs + next-step banner"]

        subgraph Tabs["tabs/ (13 modules)"]
            Compliance["compliance.py"]
            Gaps["gaps.py<br/>+ Teaming Strategy view"]
            Outline["outline.py"]
            Team["team.py<br/>+ AI Composer dialog"]
            Cost["cost.py<br/>service-line branched"]
            CostReview["cost_review.py<br/>service-line branched"]
            Draft["draft.py<br/>+ placeholder dialogs"]
            Findings["findings.py"]
            FinalPolish["final_polish.py<br/>+ View-final-draft dialog"]
            CompletedDraft["completed_draft.py"]
            Submission["submission_checklist.py"]
            Timeline["timeline.py"]
            Spend["spend.py"]
        end
    end

    Layout --> Theme
    Pages --> Layout
    Pages --> Shared
    Pages --> Tabs
    Compliance --> Shared
    Gaps --> Shared
    Outline --> Shared
    Team --> Shared
    Cost -.->|lazy shim| Pages
    CostReview -.->|lazy shim| Pages
    Draft -.->|lazy shim| Pages
    Findings -.->|lazy shim| Pages
    CompletedDraft --> Shared
    FinalPolish --> Shared
    Submission --> Shared
    Timeline --> Theme
    Spend --> Shared

    classDef brand fill:#DBEAFE,stroke:#1E40AF
    classDef shared fill:#F3F4F6,stroke:#4B5563
    classDef tab fill:#FEF9C3,stroke:#A16207

    class Layout,Theme brand
    class Shared,Pages shared
    class Compliance,Gaps,Outline,Team,Cost,CostReview,Draft,Findings,FinalPolish,CompletedDraft,Submission,Timeline,Spend tab
```

**Notes:**
- **Solid arrows** = top-level imports. **Dashed arrows** = lazy `_pages_helper(name)` shims — used by tabs that reference render helpers still living in `pages.py` (cycle-safe because the lookup happens at call time, not load time).
- **`_PROPOSAL_REVIEW_TABS` constant** in `pages.py` is the single source of truth for tab order and badges. Adding a new tab is one entry there + (if it has a badge) a count query in `_compute_tab_badges`.
- **`_theme.py`** centralizes the Quadratic Digital brand tokens (NAVY `#1F3A5F`, CYAN `#12A5D5`) so layout + tabs share the same constants.

---

## 5. Cross-Cutting: Cost Tracking

Every LLM call records to `agent_runs`. The Spend tab aggregates by stage and by agent.

```mermaid
flowchart LR
    Agent["Any agent<br/>(e.g. writer_team)"]
    LLM["app/services/llm.py<br/>call_tool_for_model()"]
    Provider["Provider SDK<br/>(anthropic / google-genai / openai)"]
    AgentRun[("agent_runs<br/>row written")]
    Spend["Spend tab<br/>aggregations:<br/>- by stage (compliance / draft / review / etc)<br/>- by agent_name (writer_team / reviewer_a / etc)<br/>- by model_used (Opus / Sonnet / Gemini-Pro / etc)"]
    Logs["INFO log line via fmt_llm_usage(usage)<br/>'in=N out=N cost=$X.XXXX'"]

    Agent --> LLM
    LLM -->|HTTPS| Provider
    Provider -->|response + usage| LLM
    LLM -->|writes| AgentRun
    LLM -->|emits| Logs
    AgentRun --> Spend

    classDef storage fill:#DBEAFE,stroke:#1E40AF
    class AgentRun storage
```

**Notes:**
- **Cost dashboard** at `app/services/cost_dashboard.py:compute_proposal_costs()` is the aggregator. Single query against `agent_runs` filtered by `proposal_id`.
- **`fmt_llm_usage(usage)`** helper centralizes the log-line format across ~17 agents — consistent observability without per-agent boilerplate.
- **`stage_name`** is derived from `agent_name` via a static map in `cost_dashboard.py`. Lets new agents slot into the right Spend bucket without code changes if their name follows existing prefix conventions.

---

## 6. Frozen Architectural Invariants (CLAUDE.md)

These bind future work. Any change here requires explicit user sign-off:

1. **No automated submission, ever.** The system drafts; humans submit.
2. **Past-performance citations only trace to `past_performance_won` / `past_performance_subbed`.** Pending or lost prior proposals can ground voice but not be cited as completed work. Reviewer A enforces this.
3. **Profile suggestions are never auto-applied.** `data/company_profile.json` mutations require explicit human approval via the Pending Profile Updates panel.
4. **No competitor proposals, no copyrighted training material.** Even FOIA-released other-firm proposals are excluded. Procurement-craft KB is limited to public-domain government guides + free APMP/Shipley abstracts + Quadratic's own house style notes.
5. **`company_profile.json` is the canonical source of truth.** Don't hardcode company facts in agent prompts; load from the profile loader.

---

## 7. File Layout Reference

```
rfp-factory/
├── alembic/versions/         # 31 migrations (0001 → 0031)
├── app/
│   ├── agents/               # 24 LLM agents (one role per file)
│   ├── jobs/                 # 13 job orchestrators (daemon-thread spawners)
│   ├── services/             # 30 domain services (DB + filesystem + LLM dispatch)
│   ├── models/               # 14 SQLAlchemy ORM modules
│   ├── core/                 # enums, profile loader, decisions
│   ├── ui/
│   │   ├── pages.py          # 6,822 lines: page handlers + shared dialogs
│   │   ├── layout.py         # page_frame() — branded header + sidebar
│   │   ├── _theme.py         # brand tokens + asset paths
│   │   ├── _shared.py        # _empty_state, _extract_section_markdown
│   │   └── tabs/             # 13 tab modules
│   ├── db/                   # SQLAlchemy session + connect listener
│   ├── config.py             # pydantic-settings (env-aware)
│   └── main.py               # entrypoint: ui.run() + static-file mount
├── assets/
│   ├── brand/                # qd-logo-*.png + favicon.ico
│   └── rfp_factory.ico       # desktop shortcut icon
├── data/                     # gitignored runtime data + tracked JSON config
│   ├── kb_documents/         # uploaded KB files (gitignored)
│   ├── rfp_packages/         # uploaded RFP files (gitignored)
│   ├── outputs/              # generated DOCX (gitignored)
│   ├── sqlite.db             # SQLite DB (gitignored)
│   ├── company_profile.json  # canonical profile (tracked)
│   ├── pricing/              # pricing config (tracked)
│   ├── internal_pricing_rules.json  # tracked
│   ├── teaming_partners.json # tracked
│   └── decisions.json        # tracked
├── docs/                     # this file + handoffs + architecture v2.0
└── scripts/                  # run_app.bat + brand asset builder + e2e tests
```
