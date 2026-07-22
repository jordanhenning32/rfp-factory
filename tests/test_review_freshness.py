from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def _bind_freshness_services(monkeypatch):
    import app.db.session as db_session
    import app.services.payment_cost_review as payment_review
    import app.services.pricing as pricing
    import app.services.review_freshness as freshness
    import app.services.service_line as service_line
    import app.services.submission_commitments as commitments

    for module in (
        payment_review,
        pricing,
        freshness,
        service_line,
        commitments,
    ):
        monkeypatch.setattr(module, "session_scope", db_session.session_scope)
    return (
        db_session,
        commitments,
        freshness,
        payment_review,
        pricing,
        service_line,
    )


def _seed_proposal(db_session, *, service_line: str) -> int:
    from app.core.enums import ProposalStatus
    from app.models import Proposal, RfpPackage

    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://freshness",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Freshness contract",
            status=ProposalStatus.DRAFT_READY,
            service_line=service_line,
        )
        db.add(proposal)
        db.flush()
        return proposal.id


def _check(commitments, proposal_id: int, key: str) -> dict:
    return next(
        item
        for item in commitments.compute_system_verified_items(proposal_id)
        if item["key"] == key
    )


def _configure_payment_data(tmp_path, monkeypatch, service_line):
    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir(exist_ok=True)
    pricing_path = pricing_dir / "payment_systems.json"
    pricing_path.write_text(
        json.dumps({
            "our_cost_basis": {
                "sponsor_acquirer_fee_bps": 8.0,
                "gateway_per_txn_usd": 0.03,
                "annualized_pci_compliance_usd": 15_000.0,
                "annualized_support_allocation_usd": 20_000.0,
                "_confirmed_by_ops_finance": False,
            },
            "fee_schedule": {"chargeback_fee_usd": 15.0},
        }),
        encoding="utf-8",
    )
    (pricing_dir / "_payment_systems_context.json").write_text(
        json.dumps({"brand": "Synthetic payment context"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(service_line, "DATA_DIR", tmp_path)
    service_line.reload_payment_systems_data()
    return pricing_path


def test_selected_scenario_invalidates_old_it_cost_review(
    inmemory_db, monkeypatch,
) -> None:
    (
        db_session,
        commitments,
        freshness,
        _payment_review,
        pricing,
        _service_line,
    ) = _bind_freshness_services(monkeypatch)
    from app.core.enums import AgentRunStatus
    from app.models import AgentRun, PricingPackage

    proposal_id = _seed_proposal(db_session, service_line="it_services")
    now = datetime.now(UTC)
    with db_session.session_scope() as db:
        db.add_all([
            PricingPackage(proposal_id=proposal_id, scenario=scenario)
            for scenario in ("LOW", "MEDIUM", "HIGH")
        ])
        # Legacy proposals have provider rows but no explicit coverage marker.
        db.add(AgentRun(
            proposal_id=proposal_id,
            agent_name="cost_reviewer:legacy-model",
            model_used="legacy-model",
            status=AgentRunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
        ))

    assert _check(commitments, proposal_id, "cost_review_run")["verified"]

    # NULL is semantically MEDIUM, so selecting MEDIUM is not a mutation of
    # the reviewed pricing basis and must not create a false invalidation.
    pricing.set_proposed_scenario(proposal_id, "MEDIUM")
    assert _check(commitments, proposal_id, "cost_review_run")["verified"]

    pricing.set_proposed_scenario(proposal_id, "HIGH")
    stale = _check(commitments, proposal_id, "cost_review_run")
    assert not stale["verified"]
    assert "stale" in stale["detail"].lower()

    freshness.record_cost_review_coverage(proposal_id, "HIGH")
    assert _check(commitments, proposal_id, "cost_review_run")["verified"]

    # Idempotent persistence leaves the matching review current.
    pricing.set_proposed_scenario(proposal_id, "HIGH")
    assert _check(commitments, proposal_id, "cost_review_run")["verified"]

    pricing.set_proposed_scenario(proposal_id, "LOW")
    assert not _check(
        commitments, proposal_id, "cost_review_run",
    )["verified"]


def test_it_cost_review_cannot_certify_superseded_snapshot(
    inmemory_db, monkeypatch,
) -> None:
    (
        db_session,
        _commitments,
        freshness,
        _payment_review,
        pricing,
        _service_line,
    ) = _bind_freshness_services(monkeypatch)
    from app.models import AgentRun, PricingPackage
    from app.services.review_freshness import COST_REVIEW_COVERAGE_AGENT

    proposal_id = _seed_proposal(db_session, service_line="it_services")
    with db_session.session_scope() as db:
        db.add_all([
            PricingPackage(proposal_id=proposal_id, scenario=scenario)
            for scenario in ("LOW", "MEDIUM", "HIGH")
        ])

    reviewed_basis = freshness.capture_it_cost_review_basis(proposal_id)
    assert reviewed_basis is not None
    pricing.set_proposed_scenario(proposal_id, "HIGH")

    assert freshness.record_cost_review_coverage(
        proposal_id,
        "MEDIUM",
        expected_basis=reviewed_basis,
    ) is False
    with db_session.session_scope() as db:
        assert db.query(AgentRun).filter(
            AgentRun.proposal_id == proposal_id,
            AgentRun.agent_name == COST_REVIEW_COVERAGE_AGENT,
        ).count() == 0

    current_basis = freshness.capture_it_cost_review_basis(proposal_id)
    assert current_basis != reviewed_basis
    assert freshness.record_cost_review_coverage(
        proposal_id,
        "HIGH",
        expected_basis=current_basis,
    ) is True


def _payment_scan(freshness) -> dict:
    return freshness.stamp_payment_market_scan_provenance({
        "pricing_structure": {
            "pricing_model": "interchange_plus",
            "pricing_model_rationale": "Transparent public-sector pricing.",
            "proposed_credit_card_markup_bps": 25,
            "proposed_per_txn_fee_usd": 0.10,
            "proposed_monthly_fee_usd": 20.0,
        },
        "volume_estimate": {
            "annual_processed_volume_low_usd": 8_000_000,
            "annual_processed_volume_midpoint_usd": 10_000_000,
            "annual_processed_volume_high_usd": 12_000_000,
            "estimated_transaction_count_annual": 100_000,
            "average_transaction_size_usd": 100.0,
            "estimation_basis": "Synthetic contract test.",
            "confidence": "medium",
        },
        "comparable_awards": [],
        "competitor_processors": [],
        "profit_math": {},
        "insufficient_data_warning": False,
        "citations": [],
    })


def _seed_payment_review(
    db_session,
    freshness,
    payment_review,
    *,
    proposal_id: int,
) -> None:
    from app.models import Proposal, ProposalSection

    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        proposal.payment_market_scan_json = json.dumps(_payment_scan(freshness))
        db.add(ProposalSection(
            proposal_id=proposal_id,
            section_id="COST-001",
            section_title="Payment Fees",
            section_order=1,
            requires_cost_analysis=True,
            draft_text_markdown="Our current payment fee narrative.",
            current_revision_number=1,
        ))

    assert payment_review.persist_payment_cost_review_data(
        proposal_id,
        {"findings": [], "bid_ready": True},
    )


def test_global_payment_cost_basis_change_stales_every_proposal(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    (
        db_session,
        commitments,
        freshness,
        payment_review,
        _pricing,
        service_line,
    ) = _bind_freshness_services(monkeypatch)

    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    (pricing_dir / "payment_systems.json").write_text(
        json.dumps({
            "our_cost_basis": {
                "sponsor_acquirer_fee_bps": 8.0,
                "gateway_per_txn_usd": 0.03,
                "annualized_pci_compliance_usd": 15_000.0,
                "annualized_support_allocation_usd": 20_000.0,
                "_confirmed_by_ops_finance": False,
            },
            "fee_schedule": {"chargeback_fee_usd": 15.0},
        }),
        encoding="utf-8",
    )
    (pricing_dir / "_payment_systems_context.json").write_text(
        json.dumps({"brand": "Synthetic payment context"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(service_line, "DATA_DIR", tmp_path)
    service_line.reload_payment_systems_data()

    first_id = _seed_proposal(db_session, service_line="payment_systems")
    second_id = _seed_proposal(db_session, service_line="payment_systems")
    for proposal_id in (first_id, second_id):
        _seed_payment_review(
            db_session,
            freshness,
            payment_review,
            proposal_id=proposal_id,
        )
        assert _check(commitments, proposal_id, "cost_build")["verified"]
        assert _check(commitments, proposal_id, "cost_review_run")["verified"]

    service_line.update_payment_cost_basis(
        proposal_id=first_id,
        sponsor_acquirer_fee_bps=11.0,
        confirmed_by_ops_finance=True,
    )

    # The file is shared. Its atomic replacement changes the current hash, so
    # both proposals fail closed even though no cross-proposal DB write occurs.
    for proposal_id in (first_id, second_id):
        assert not _check(commitments, proposal_id, "cost_build")["verified"]
        assert not _check(
            commitments, proposal_id, "cost_review_run",
        )["verified"]

    # The UI refreshes only the active proposal's cheap profit math. That
    # restores its cost-build provenance, while its old adversarial review and
    # every untouched proposal remain stale.
    assert service_line.recompute_payment_profit_math(first_id)
    assert _check(commitments, first_id, "cost_build")["verified"]
    assert not _check(
        commitments, first_id, "cost_review_run",
    )["verified"]
    assert not _check(commitments, second_id, "cost_build")["verified"]

    assert payment_review.persist_payment_cost_review_data(
        first_id,
        {"findings": [], "bid_ready": True},
    )
    assert _check(commitments, first_id, "cost_review_run")["verified"]
    assert not _check(
        commitments, second_id, "cost_review_run",
    )["verified"]

    # Pricing-model selection is another input to the payment reviewer.
    service_line.set_selected_pricing_model(first_id, "flat_rate")
    assert not _check(
        commitments, first_id, "cost_review_run",
    )["verified"]


def test_payment_review_rejects_provenance_from_old_inputs(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    (
        db_session,
        _commitments,
        freshness,
        payment_review,
        _pricing,
        service_line,
    ) = _bind_freshness_services(monkeypatch)
    from app.models import Proposal, ProposalSection

    _configure_payment_data(tmp_path, monkeypatch, service_line)
    proposal_id = _seed_proposal(db_session, service_line="payment_systems")
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        proposal.payment_market_scan_json = json.dumps(_payment_scan(freshness))
        db.add(ProposalSection(
            proposal_id=proposal_id,
            section_id="COST-RACE",
            section_title="Payment Fees",
            section_order=1,
            requires_cost_analysis=True,
            draft_text_markdown="Reviewed fee narrative.",
            current_revision_number=1,
        ))

    reviewed_provenance = freshness.build_payment_review_provenance(proposal_id)
    assert reviewed_provenance is not None
    service_line.set_selected_pricing_model(proposal_id, "flat_rate")

    assert payment_review.persist_payment_cost_review_data(
        proposal_id,
        {"findings": [], "bid_ready": True},
        reviewed_provenance=reviewed_provenance,
    ) is False
    with db_session.session_scope() as db:
        assert db.get(Proposal, proposal_id).payment_cost_review_findings_json is None

    current_provenance = freshness.build_payment_review_provenance(proposal_id)
    assert current_provenance != reviewed_provenance
    assert payment_review.persist_payment_cost_review_data(
        proposal_id,
        {"findings": [], "bid_ready": True},
        reviewed_provenance=current_provenance,
    ) is True
    with db_session.session_scope() as db:
        stored = json.loads(
            db.get(Proposal, proposal_id).payment_cost_review_findings_json
        )
    assert stored[freshness.PAYMENT_REVIEW_PROVENANCE_KEY] == current_provenance


def test_payment_market_profit_rejects_superseded_cost_basis(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    (
        db_session,
        _commitments,
        freshness,
        _payment_review,
        _pricing,
        service_line,
    ) = _bind_freshness_services(monkeypatch)
    from app.jobs import payment_market_researcher as market_job
    from app.models import Proposal

    _configure_payment_data(tmp_path, monkeypatch, service_line)
    monkeypatch.setattr(market_job, "session_scope", db_session.session_scope)
    proposal_id = _seed_proposal(db_session, service_line="payment_systems")
    used_provenance = freshness.current_payment_cost_basis_provenance()

    class _ComputedResult:
        def to_json_dict(self):
            return {"profit_math": {"annual_net_profit_midpoint_usd": 1.0}}

    # This mutation lands after deterministic profit math used the old snapshot
    # but before its persistence boundary.
    service_line.update_payment_cost_basis(
        proposal_id=proposal_id,
        sponsor_acquirer_fee_bps=12.0,
    )
    assert freshness.current_payment_cost_basis_provenance() != used_provenance
    assert market_job._persist_result(
        proposal_id,
        _ComputedResult(),
        cost_basis_provenance=used_provenance,
    ) is False
    with db_session.session_scope() as db:
        assert db.get(Proposal, proposal_id).payment_market_scan_json is None


def test_archive_waits_until_cost_basis_replace_finishes(
    tmp_path, monkeypatch,
) -> None:
    import app.models  # noqa: F401 -- register all models on metadata
    from app.core.enums import ProposalStatus
    from app.db.base import Base
    from app.models import Proposal, RfpPackage
    from app.services import service_line, workflow

    db_path = tmp_path / "archive-cost-basis-race.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(
        bind=engine,
        autoflush=True,
        autocommit=False,
        future=True,
    )

    @contextmanager
    def _session_scope():
        db = TestSession()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with _session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://archive-cost-basis-race",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Archive serialization",
            status=ProposalStatus.SUBMITTED,
            service_line="payment_systems",
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id

    _configure_payment_data(tmp_path, monkeypatch, service_line)
    monkeypatch.setattr(service_line, "session_scope", _session_scope)
    monkeypatch.setattr(workflow, "session_scope", _session_scope)

    replace_entered = threading.Event()
    allow_replace = threading.Event()
    archive_attempted = threading.Event()
    archive_finished = threading.Event()
    real_replace = service_line.os.replace
    real_workflow_lock = workflow.proposal_write_lock

    def _paused_replace(source, target) -> None:
        replace_entered.set()
        assert allow_replace.wait(timeout=2)
        real_replace(source, target)

    @contextmanager
    def _observed_workflow_lock(target_id: int):
        archive_attempted.set()
        with real_workflow_lock(target_id):
            yield

    monkeypatch.setattr(service_line.os, "replace", _paused_replace)
    monkeypatch.setattr(workflow, "proposal_write_lock", _observed_workflow_lock)

    update_errors: list[BaseException] = []
    archive_errors: list[BaseException] = []
    archive_result: list[dict] = []

    def _update() -> None:
        try:
            service_line.update_payment_cost_basis(
                proposal_id=proposal_id,
                sponsor_acquirer_fee_bps=13.0,
            )
        except BaseException as exc:
            update_errors.append(exc)

    def _archive() -> None:
        try:
            archive_result.append(workflow.archive_proposal(proposal_id))
        except BaseException as exc:
            archive_errors.append(exc)
        finally:
            archive_finished.set()

    update_thread = threading.Thread(target=_update, name="cost-basis-update")
    archive_thread = threading.Thread(target=_archive, name="archive")
    update_thread.start()
    try:
        assert replace_entered.wait(timeout=2)
        archive_thread.start()
        assert archive_attempted.wait(timeout=2)
        # Archive has reached the shared serialization boundary, but cannot
        # pass the mutability check until the authorized replace commits.
        assert not archive_finished.wait(timeout=0.2)
    finally:
        allow_replace.set()
        update_thread.join(timeout=2)
        if archive_thread.ident is not None:
            archive_thread.join(timeout=2)

    assert not update_thread.is_alive()
    assert not archive_thread.is_alive()
    assert update_errors == []
    assert archive_errors == []
    assert archive_result == [{"ok": True, "reason": None, "blockers": []}]
    with _session_scope() as db:
        assert db.get(Proposal, proposal_id).status == ProposalStatus.ARCHIVED
    assert service_line.get_payment_cost_basis()["sponsor_acquirer_fee_bps"] == 13.0
    engine.dispose()
