"""Per-proposal cost rollup for the Spend tab.

Aggregates `agent_runs.cost_usd` and token counts by logical pipeline
stage (Compliance Matrix, Shortfall, Teaming, Outline, Writer, Reviewer
A/B, Consistency, etc.). Pure read-only over the existing agent_runs
table — no DB changes, no LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select

from app.db.session import session_scope
from app.models import AgentRun

# Agent-name → logical-stage mapping. Keep this in sync with the
# agent_name values used in `call_tool_for_model(...)` calls across the
# codebase. Values not in this map fall through to "Other" so a new
# agent doesn't silently disappear from the dashboard.
_AGENT_TO_STAGE: dict[str, str] = {
    "intake_metadata": "Intake metadata",
    "compliance_matrix": "Compliance Matrix",
    "compliance_validator": "Compliance Matrix",
    "compliance_validator_retry": "Compliance Matrix",
    "compliance_validator_fallback": "Compliance Matrix",
    "compliance_completeness": "Compliance Matrix",
    "compliance_completeness_retry": "Compliance Matrix",
    "compliance_completeness_fallback": "Compliance Matrix",
    "shortfall_strategist": "Shortfall Strategist",
    "teaming_researcher_research": "Teaming Researcher",
    "teaming_researcher_structure": "Teaming Researcher",
    "outline_agent": "Outline",
    "writer_team": "Writer Team",
    "reviewer_a": "Reviewer A",
    "reviewer_b": "Reviewer B",
    "consistency_checker": "Consistency Check",
    "grounding_check_credentials": "Credential Grounding",
    "needs_human_advisor": "Needs-Human Advisor",
    "lesson_extractor": "Lesson Extractor",
    "kb_facts_personnel": "KB Processing",
    "kb_facts_corporate": "KB Processing",
    "kb_facts_past_performance": "KB Processing",
    "kb_classify": "KB Processing",
}

# Display order for stages — tracks the actual pipeline flow so the
# dashboard reads top-to-bottom like a runbook.
_STAGE_ORDER: tuple[str, ...] = (
    "Intake metadata",
    "Compliance Matrix",
    "Shortfall Strategist",
    "Teaming Researcher",
    "Outline",
    "Writer Team",
    "Credential Grounding",
    "Reviewer A",
    "Reviewer B",
    "Consistency Check",
    "Needs-Human Advisor",
    "Lesson Extractor",
    "KB Processing",
    "Other",
)


def stage_for_agent(agent_name: str) -> str:
    """Resolve an agent_name to its logical-stage label. Unknown agents
    bucket into 'Other' so new ones surface in the dashboard without a
    code change."""
    return _AGENT_TO_STAGE.get(agent_name, "Other")


@dataclass
class AgentBreakdown:
    """One row in the detailed per-agent table."""

    agent_name: str
    stage: str
    n_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class StageRollup:
    """One row in the stage-level summary."""

    stage: str
    n_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ProposalCostSummary:
    """Aggregate of every cost-bearing AgentRun for one proposal."""

    proposal_id: int
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_calls: int = 0
    stages: list[StageRollup] = field(default_factory=list)
    agents: list[AgentBreakdown] = field(default_factory=list)
    first_run_at: datetime | None = None
    last_run_at: datetime | None = None


def compute_proposal_costs(proposal_id: int) -> ProposalCostSummary:
    """Pull every AgentRun for `proposal_id` and aggregate by agent and
    by logical stage. Excludes `_stage` and `_review_coverage` rows — those
    are pipeline/audit markers, not LLM calls, and cost $0 by convention.
    """
    summary = ProposalCostSummary(proposal_id=proposal_id)

    with session_scope() as db:
        # Per-agent rollup query — does the heavy lifting in SQL so we
        # don't drag every row across the wire on a long-running proposal.
        rows = db.execute(
            select(
                AgentRun.agent_name,
                func.count(AgentRun.id).label("n_calls"),
                func.sum(AgentRun.input_tokens).label("input_tokens"),
                func.sum(AgentRun.output_tokens).label("output_tokens"),
                func.sum(AgentRun.cost_usd).label("cost_usd"),
                func.min(AgentRun.created_at).label("first_at"),
                func.max(AgentRun.created_at).label("last_at"),
            )
            .where(AgentRun.proposal_id == proposal_id)
            .where(AgentRun.agent_name.notin_(["_stage", "_review_coverage"]))
            .group_by(AgentRun.agent_name)
        ).all()

    if not rows:
        return summary

    # Aggregate at the agent level + at the stage level in one pass.
    by_stage: dict[str, StageRollup] = {}
    for r in rows:
        agent_name = r.agent_name or "?"
        stage = stage_for_agent(agent_name)
        n_calls = int(r.n_calls or 0)
        in_tok = int(r.input_tokens or 0)
        out_tok = int(r.output_tokens or 0)
        cost = float(r.cost_usd or 0.0)

        summary.agents.append(
            AgentBreakdown(
                agent_name=agent_name,
                stage=stage,
                n_calls=n_calls,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
            )
        )

        bucket = by_stage.setdefault(stage, StageRollup(stage=stage))
        bucket.n_calls += n_calls
        bucket.input_tokens += in_tok
        bucket.output_tokens += out_tok
        bucket.cost_usd += cost

        summary.total_calls += n_calls
        summary.total_input_tokens += in_tok
        summary.total_output_tokens += out_tok
        summary.total_cost_usd += cost

        if r.first_at is not None:
            if summary.first_run_at is None or r.first_at < summary.first_run_at:
                summary.first_run_at = r.first_at
        if r.last_at is not None:
            if summary.last_run_at is None or r.last_at > summary.last_run_at:
                summary.last_run_at = r.last_at

    # Order stages per pipeline flow; tail with anything unmapped so a
    # newly-added agent shows up under "Other" until we map it.
    ordered_stages: list[StageRollup] = []
    seen: set[str] = set()
    for stage in _STAGE_ORDER:
        if stage in by_stage:
            ordered_stages.append(by_stage[stage])
            seen.add(stage)
    for stage, rollup in sorted(by_stage.items()):
        if stage not in seen:
            ordered_stages.append(rollup)
    summary.stages = ordered_stages

    # Sort the per-agent detail by cost desc so the biggest spenders
    # are at the top of the breakdown table.
    summary.agents.sort(key=lambda a: a.cost_usd, reverse=True)

    return summary


__all__ = [
    "compute_proposal_costs",
    "stage_for_agent",
    "ProposalCostSummary",
    "StageRollup",
    "AgentBreakdown",
]
