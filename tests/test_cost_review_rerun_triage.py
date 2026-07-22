"""Cost-review reruns preserve human triage for the same logical issue."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session


def _seed_proposal(engine, *, with_pricing: bool) -> int:
    from app.core.enums import ProposalRole, ProposalStatus
    from app.models import PricingPackage, Proposal, RfpPackage

    with Session(engine) as db:
        package = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://cost-review-rerun",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Cost Review Rerun",
            role=ProposalRole.PRIME,
            status=ProposalStatus.DRAFT_READY,
            service_line=("it_services" if with_pricing else "payment_systems"),
        )
        db.add(proposal)
        db.flush()
        if with_pricing:
            db.add_all([
                PricingPackage(proposal_id=proposal.id, scenario=scenario)
                for scenario in ("LOW", "MEDIUM", "HIGH")
            ])
        proposal_id = proposal.id
        db.commit()
        return proposal_id


def _labor_finding(
    *,
    severity: str,
    category: str,
    subject: str,
    text: str,
    change: str,
    scenarios: list[str],
):
    from app.agents.cost_reviewer import CostReviewFinding

    return CostReviewFinding(
        severity=severity,
        category=category,
        subject=subject,
        finding_text=text,
        recommended_change=change,
        scenarios_affected=scenarios,
        alternative_scenarios=[],
    )


def test_it_cost_review_rerun_preserves_matching_triage_only(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.agents.cost_reviewer import CostReviewResult
    from app.services import cost_reviewer

    monkeypatch.setattr(cost_reviewer, "session_scope", db_session.session_scope)
    proposal_id = _seed_proposal(inmemory_db, with_pricing=True)

    initial = CostReviewResult(findings=[
        _labor_finding(
            severity="MAJOR",
            category="unrealistic_hours",
            subject="Hours gap",
            text="Hours are too low.",
            change="Add 200 hours.",
            scenarios=["LOW", "MEDIUM"],
        ),
        _labor_finding(
            severity="MINOR",
            category="margin_pressure",
            subject="Margin issue",
            text="Margin needs explanation.",
            change="Explain the margin.",
            scenarios=["HIGH"],
        ),
        _labor_finding(
            severity="MINOR",
            category="odc_missing",
            subject="Old travel issue",
            text="Travel was omitted.",
            change="Add travel.",
            scenarios=["LOW"],
        ),
    ])
    assert cost_reviewer.upsert_cost_review_findings(
        proposal_id=proposal_id,
        result=initial,
    ) == 4

    rows = cost_reviewer.get_cost_review_findings_snapshot(proposal_id)
    accepted_ids = [
        row["id"] for row in rows
        if row["category"] == "unrealistic_hours"
    ]
    rejected_ids = [
        row["id"] for row in rows
        if row["category"] == "margin_pressure"
    ]
    removed_ids = [
        row["id"] for row in rows
        if row["category"] == "odc_missing"
    ]
    assert cost_reviewer.update_cost_review_finding_action(
        finding_ids=accepted_ids,
        user_action="accepted",
        user_note="Use the user's 240-hour correction.",
    ) == 2
    assert cost_reviewer.update_cost_review_finding_action(
        finding_ids=rejected_ids,
        user_action="rejected",
        user_note="The margin is already justified in Attachment B.",
    ) == 1
    assert cost_reviewer.update_cost_review_finding_action(
        finding_ids=removed_ids,
        user_action="accepted",
        user_note="This triage must disappear with the finding.",
    ) == 1

    rerun = CostReviewResult(findings=[
        _labor_finding(
            severity="MAJOR",
            category="unrealistic_hours",
            subject="Hours gap",
            text="  hours   are TOO low.  ",
            change="Add 250 hours.",
            scenarios=["HIGH", "LOW"],
        ),
        _labor_finding(
            severity="MINOR",
            category="margin_pressure",
            subject="Margin issue",
            text="Margin needs explanation.",
            change="Add a clearer explanation.",
            scenarios=["MEDIUM"],
        ),
        _labor_finding(
            severity="MINOR",
            category="phase_gap",
            subject="New transition issue",
            text="Transition labor is missing.",
            change="Add transition labor.",
            scenarios=["HIGH"],
        ),
    ])
    assert cost_reviewer.upsert_cost_review_findings(
        proposal_id=proposal_id,
        result=rerun,
    ) == 4

    after = cost_reviewer.get_cost_review_findings_snapshot(proposal_id)
    accepted = [
        row for row in after if row["category"] == "unrealistic_hours"
    ]
    assert {row["scenario"] for row in accepted} == {"LOW", "HIGH"}
    assert {row["user_action"] for row in accepted} == {"accepted"}
    assert {row["user_note"] for row in accepted} == {
        "Use the user's 240-hour correction."
    }
    assert {row["auto_actioned"] for row in accepted} == {False}

    [rejected] = [
        row for row in after if row["category"] == "margin_pressure"
    ]
    assert rejected["user_action"] == "rejected"
    assert rejected["user_note"] == (
        "The margin is already justified in Attachment B."
    )

    [new_finding] = [
        row for row in after if row["category"] == "phase_gap"
    ]
    assert new_finding["user_action"] == "pending"
    assert new_finding["user_note"] is None
    assert new_finding["auto_actioned"] is False
    assert not any(row["category"] == "odc_missing" for row in after)


def _payment_finding(
    *,
    finding_id: str,
    category: str,
    text: str,
    fix: str,
    quote: str,
):
    from app.agents.payment_cost_reviewer import PaymentCostReviewFinding

    return PaymentCostReviewFinding(
        finding_id=finding_id,
        section_id="SEC-005",
        section_title="Fee Narrative",
        severity="MAJOR",
        category=category,
        finding_text=text,
        suggested_fix=fix,
        cited_quote=quote,
    )


def test_payment_cost_review_rerun_matches_content_not_sequential_id(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.agents.payment_cost_reviewer import PaymentCostReviewResult
    from app.jobs import payment_cost_reviewer as payment_job
    from app.services import payment_cost_review

    monkeypatch.setattr(
        payment_cost_review,
        "session_scope",
        db_session.session_scope,
    )
    proposal_id = _seed_proposal(inmemory_db, with_pricing=False)

    initial = PaymentCostReviewResult(
        findings=[
            _payment_finding(
                finding_id="PCR-001",
                category="RATE_DRIFT",
                text="The quoted rate is wrong.",
                fix="Use 22 bps.",
                quote="We propose 25 bps.",
            ),
            _payment_finding(
                finding_id="PCR-002",
                category="MISSING_DISCLOSURE",
                text="The PCI roadmap is missing.",
                fix="Add the Level 2 roadmap.",
                quote="We maintain PCI Level 3.",
            ),
            _payment_finding(
                finding_id="PCR-003",
                category="BRAND_VOICE_DRIFT",
                text="The old brand framing is wrong.",
                fix="Use the approved brand framing.",
                quote="Fitness billing leader.",
            ),
        ],
        overall_assessment="Three findings.",
        bid_ready=False,
        sections_reviewed=["SEC-005"],
    )
    payment_job._persist_result(proposal_id, initial)
    assert payment_cost_review.update_payment_finding_action(
        proposal_id,
        "PCR-001",
        action="accepted",
        user_note="Use the negotiated 21 bps rate.",
    ) is not None
    assert payment_cost_review.update_payment_finding_action(
        proposal_id,
        "PCR-002",
        action="rejected",
        user_note="The roadmap is disclosed in SEC-006.",
    ) is not None
    assert payment_cost_review.update_payment_finding_action(
        proposal_id,
        "PCR-003",
        action="accepted",
        user_note="This removed finding must not survive.",
    ) is not None

    # The result order changed, so both retained issues receive different
    # sequential IDs. Identity must come from their logical content instead.
    rerun = PaymentCostReviewResult(
        findings=[
            _payment_finding(
                finding_id="PCR-001",
                category="MISSING_DISCLOSURE",
                text="The PCI roadmap is missing.",
                fix="Clarify the Level 2 date.",
                quote="We maintain PCI Level 3.",
            ),
            _payment_finding(
                finding_id="PCR-002",
                category="RATE_DRIFT",
                text="  the quoted  RATE is wrong. ",
                fix="Use the current 22 bps recommendation.",
                quote="We propose 25 bps.",
            ),
            _payment_finding(
                finding_id="PCR-003",
                category="NUMERIC_DRIFT",
                text="The annual volume is wrong.",
                fix="Use the persisted annual volume.",
                quote="$50 million annual volume.",
            ),
        ],
        overall_assessment="Two retained issues and one new issue.",
        bid_ready=False,
        sections_reviewed=["SEC-005"],
    )
    payment_job._persist_result(proposal_id, rerun)

    data = payment_cost_review.get_payment_cost_review_data(proposal_id)
    by_category = {
        finding["category"]: finding
        for finding in data["findings"]
    }
    assert by_category["RATE_DRIFT"]["finding_id"] == "PCR-002"
    assert by_category["RATE_DRIFT"]["user_action"] == "accepted"
    assert by_category["RATE_DRIFT"]["user_note"] == (
        "Use the negotiated 21 bps rate."
    )
    assert by_category["MISSING_DISCLOSURE"]["finding_id"] == "PCR-001"
    assert by_category["MISSING_DISCLOSURE"]["user_action"] == "rejected"
    assert by_category["MISSING_DISCLOSURE"]["user_note"] == (
        "The roadmap is disclosed in SEC-006."
    )
    assert by_category["NUMERIC_DRIFT"]["user_action"] == "pending"
    assert by_category["NUMERIC_DRIFT"]["user_note"] is None
    assert "BRAND_VOICE_DRIFT" not in by_category
    assert data["overall_assessment"] == (
        "Two retained issues and one new issue."
    )
