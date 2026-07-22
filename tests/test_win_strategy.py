from __future__ import annotations

import json
import threading
from datetime import UTC, datetime


def _seed_strategy_proposal():
    from app.core.enums import (
        FindingCategory,
        FindingSeverity,
        GapSeverity,
        ProposalRole,
        ProposalStatus,
        RequirementCategory,
        RequirementType,
    )
    from app.db.session import session_scope
    from app.models import (
        ComplianceMatrixItem,
        GapAnalysis,
        PricingPackage,
        Proposal,
        ProposalSection,
        ProposalTeamMember,
        ReviewerFinding,
        RfpPackage,
    )

    criteria = {
        "evaluation_method": "trade_off",
        "factors": [
            {
                "factor_id": "F1",
                "factor_name": "Technical Approach",
                "weight_pct": 60,
                "evidence_required": "Specific approach and proof.",
                "subfactors": [],
            },
            {
                "factor_id": "F2",
                "factor_name": "Risk",
                "weight_pct": 40,
                "evidence_required": "Risk mitigations.",
                "subfactors": [],
            },
        ],
        "section_l_to_m_map": {"REQ-001": ["F1"], "REQ-002": ["F2"]},
    }

    with session_scope() as db:
        pkg = RfpPackage(uploaded_by="test", uploaded_at=datetime.now(UTC), storage_dir="test")
        db.add(pkg)
        db.flush()
        proposal = Proposal(
            rfp_package_id=pkg.id,
            title="State payment modernization",
            agency="Example Public Agency",
            role=ProposalRole.PRIME,
            status=ProposalStatus.DRAFT_READY,
            evaluation_criteria_json=json.dumps(criteria),
            proposed_scenario="MEDIUM",
        )
        db.add(proposal)
        db.flush()

        req1 = ComplianceMatrixItem(
            proposal_id=proposal.id,
            requirement_id="REQ-001",
            requirement_text="Offeror shall describe cloud modernization and payment integration.",
            source_doc="rfp.pdf",
            requirement_type=RequirementType.SHALL,
            category=RequirementCategory.TECHNICAL,
        )
        req2 = ComplianceMatrixItem(
            proposal_id=proposal.id,
            requirement_id="REQ-002",
            requirement_text="Offeror shall identify delivery risks and mitigation controls.",
            source_doc="rfp.pdf",
            requirement_type=RequirementType.SHALL,
            category=RequirementCategory.MANAGEMENT,
        )
        db.add_all([req1, req2])
        db.flush()

        sec1 = ProposalSection(
            proposal_id=proposal.id,
            section_id="SEC-001",
            section_title="Technical Approach",
            section_order=1,
            section_brief="This section targets F1 and explains public payment modernization.",
            draft_text_markdown="Quadratic will modernize payment integrations with cloud controls. [^cite-1]",
            citations_json=[
                {
                    "marker": "cite-1",
                    "claim": "Cloud payment modernization",
                    "source_kb_doc": "company_profile.past_performance",
                    "confidence": "HIGH",
                }
            ],
            compliance_items_addressed_json=["REQ-001"],
        )
        sec2 = ProposalSection(
            proposal_id=proposal.id,
            section_id="SEC-002",
            section_title="Risk Management",
            section_order=2,
            section_brief="This section targets F2 and explains risk mitigations.",
            draft_text_markdown="",
            citations_json=[],
            compliance_items_addressed_json=["REQ-002"],
        )
        db.add_all([sec1, sec2])
        db.flush()

        db.add(
            GapAnalysis(
                proposal_id=proposal.id,
                requirement_id_fk=req2.id,
                gap_id="GAP-001",
                gap_severity=GapSeverity.MAJOR,
                gap_description="Risk mitigation detail is incomplete.",
                current_state="No mitigation narrative yet.",
                mitigation_options_json=[
                    {
                        "approach": "custom-build",
                        "proposal_language_draft": "Quadratic will use a risk register and escalation cadence.",
                        "honesty_check": "Supported by delivery process.",
                    }
                ],
                recommended_mitigation_index=0,
            )
        )
        db.add(
            ReviewerFinding(
                proposal_section_id=sec1.id,
                reviewer_agent="A",
                pass_number=1,
                severity=FindingSeverity.MAJOR,
                category=FindingCategory.WEAK_PERSUASION,
                finding_text="Technical approach needs a sharper evaluator proof point.",
            )
        )

        for scenario, price, margin in [
            ("LOW", 900000, 14.0),
            ("MEDIUM", 1100000, 24.0),
            ("HIGH", 1300000, 30.0),
        ]:
            db.add(
                PricingPackage(
                    proposal_id=proposal.id,
                    scenario=scenario,
                    loaded_labor_cost=700000,
                    total_proposed_price=price,
                    pnl_projection_json={"gross_margin_pct": margin},
                    vs_market_position="in_band",
                    bid_recommendation="bid",
                )
            )
        db.add(
            ProposalTeamMember(
                proposal_id=proposal.id,
                role_name="Project Manager",
                person_kind="named",
                assigned_person="Alex Rivera",
                labor_category="Project Manager III",
                time_allocation_pct=50,
                bio_summary="Public-sector delivery lead.",
            )
        )
        return proposal.id


def test_evaluator_scorecard_persists_and_flags_factor_risk(inmemory_db):
    proposal_id = _seed_strategy_proposal()
    from app.db.session import session_scope
    from app.models import Proposal
    from app.services.win_strategy import generate_evaluator_scorecard

    scorecard = generate_evaluator_scorecard(proposal_id)

    assert scorecard["method"] == "trade_off"
    assert len(scorecard["factors"]) == 2
    risk_factor = next(f for f in scorecard["factors"] if f["factor_id"] == "F2")
    assert risk_factor["readiness_band"] in {"At Risk", "Not Ready"}
    assert risk_factor["unresolved_gap_ids"] == ["GAP-001"]

    with session_scope() as db:
        persisted = db.get(Proposal, proposal_id).evaluator_scorecard_json
    assert json.loads(persisted)["overall_score"] == scorecard["overall_score"]


def test_generate_all_win_strategy_outputs_are_structured(inmemory_db):
    proposal_id = _seed_strategy_proposal()
    from app.services.win_strategy import generate_all_win_strategy

    result = generate_all_win_strategy(proposal_id)

    assert result["evaluator_scorecard"]["factors"]
    assert result["win_themes"]["themes"]
    assert result["past_performance_matches"]["top_citable_projects"]
    assert result["price_to_win"]["recommended_scenario"] == "MEDIUM"
    assert result["red_team_findings"]["summary"]["major"] >= 1
    assert len(result["graphics_tables"]["artifacts"]) >= 6


def test_writer_strategy_block_contains_generated_artifacts(inmemory_db):
    proposal_id = _seed_strategy_proposal()
    from app.services.win_strategy import (
        format_win_strategy_block_for_writer,
        generate_all_win_strategy,
    )

    generate_all_win_strategy(proposal_id)
    block = format_win_strategy_block_for_writer(proposal_id)

    assert "EVALUATOR SCORECARD" in block
    assert "APPROVED WIN THEMES" in block
    assert "PAST PERFORMANCE MATCH PRIORITY" in block
    assert "PRICE-TO-WIN POSTURE" in block
    assert "RED TEAM WATCH ITEMS" in block
    assert "RECOMMENDED TABLES" in block


def test_repeated_generation_does_not_spawn_threads_or_agent_runs(inmemory_db):
    proposal_id = _seed_strategy_proposal()
    from app.db.session import session_scope
    from app.models import AgentRun
    from app.services.win_strategy import generate_all_win_strategy

    before_threads = {t.ident for t in threading.enumerate()}
    with session_scope() as db:
        before_runs = db.query(AgentRun).count()

    for _ in range(3):
        generate_all_win_strategy(proposal_id)

    after_threads = {t.ident for t in threading.enumerate()}
    with session_scope() as db:
        after_runs = db.query(AgentRun).count()

    assert after_runs == before_runs
    assert after_threads == before_threads
