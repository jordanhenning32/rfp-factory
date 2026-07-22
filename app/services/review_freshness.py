"""Freshness evidence for submission-critical review steps.

The existing schema already has an append-only ``agent_runs`` audit table and
JSON payloads for the payment flow.  This module uses those two stores to bind
review evidence to the inputs it actually covered, without a database
migration.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import AgentRunStatus
from app.db.session import session_scope
from app.models import (
    AgentRun,
    PricingPackage,
    PricingPackageLine,
    Proposal,
    ProposalSection,
)
from app.services.proposal_access import (
    acquire_proposal_write_fence,
    proposal_write_lock,
)

COST_REVIEW_COVERAGE_AGENT = "_cost_review_coverage"
COST_REVIEW_INVALIDATION_AGENT = "_cost_review_invalidation"
COST_REVIEW_COVERAGE_VERSION = "cr2"

PAYMENT_COST_BASIS_PROVENANCE_KEY = "_cost_basis_provenance"
PAYMENT_COST_BASIS_PROVENANCE_VERSION = "pcb1"
PAYMENT_REVIEW_PROVENANCE_KEY = "_review_provenance"
PAYMENT_REVIEW_PROVENANCE_VERSION = "pcr1"

_PAYMENT_COST_BASIS_DEFAULTS: dict[str, float] = {
    "sponsor_acquirer_fee_bps": 8.0,
    "gateway_per_txn_usd": 0.03,
    "annualized_pci_compliance_usd": 15000.0,
    "annualized_support_allocation_usd": 20000.0,
}


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def invalidate_cost_review(
    db: Session,
    proposal_id: int,
    *,
    reason: str,
) -> None:
    """Append an audit marker that makes all older cost reviews stale.

    The marker is written in the same transaction as the pricing mutation, so
    readiness can compare monotonically increasing AgentRun ids rather than
    relying on database timestamp precision.
    """
    now = datetime.now(UTC)
    db.add(AgentRun(
        proposal_id=proposal_id,
        agent_name=COST_REVIEW_INVALIDATION_AGENT,
        model_used=None,
        prompt_version=COST_REVIEW_COVERAGE_VERSION,
        status=AgentRunStatus.CANCELLED,
        started_at=now,
        completed_at=now,
        error_text=reason[:2000],
    ))


def _it_cost_review_basis(
    db: Session,
    proposal_id: int,
) -> dict[str, Any] | None:
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        return None
    scenario = (proposal.proposed_scenario or "MEDIUM").upper().strip()
    invalidation_id = int(db.scalar(
        select(func.max(AgentRun.id)).where(
            AgentRun.proposal_id == proposal_id,
            AgentRun.agent_name == COST_REVIEW_INVALIDATION_AGENT,
        )
    ) or 0)
    packages = db.execute(
        select(PricingPackage)
        .where(PricingPackage.proposal_id == proposal_id)
        .order_by(PricingPackage.scenario, PricingPackage.id)
    ).scalars().all()
    package_payload: list[dict[str, Any]] = []
    for package in packages:
        package_data = {
            column.name: getattr(package, column.name)
            for column in PricingPackage.__table__.columns
        }
        lines = db.execute(
            select(PricingPackageLine)
            .where(PricingPackageLine.pricing_package_id == package.id)
            .order_by(PricingPackageLine.id)
        ).scalars().all()
        package_data["lines"] = [
            {
                column.name: getattr(line, column.name)
                for column in PricingPackageLine.__table__.columns
            }
            for line in lines
        ]
        package_payload.append(package_data)
    fingerprint = _canonical_hash({
        "scenario": scenario,
        "invalidation_id": invalidation_id,
        "packages": package_payload,
    })
    return {
        "version": COST_REVIEW_COVERAGE_VERSION,
        "scenario": scenario,
        "invalidation_id": invalidation_id,
        "sha256": fingerprint,
    }


def capture_it_cost_review_basis(
    proposal_id: int,
) -> dict[str, Any] | None:
    """Snapshot the exact persisted pricing generation a review will cover."""
    with session_scope() as db:
        return _it_cost_review_basis(db, proposal_id)


def _cost_review_coverage_prompt_version(basis: dict[str, Any]) -> str:
    return (
        f"{COST_REVIEW_COVERAGE_VERSION}:"
        f"{basis['scenario']}:"
        f"{basis['sha256'][:20]}"
    )


def record_cost_review_coverage(
    proposal_id: int,
    scenario: str,
    *,
    expected_basis: dict[str, Any] | None = None,
) -> bool:
    """Certify a review only if its captured pricing generation is current."""
    now = datetime.now(UTC)
    with proposal_write_lock(proposal_id):
        with session_scope() as db:
            acquire_proposal_write_fence(db, proposal_id)
            current_basis = _it_cost_review_basis(db, proposal_id)
            if current_basis is None:
                return False
            if current_basis["scenario"] != scenario.upper().strip():
                return False
            if expected_basis is not None and current_basis != expected_basis:
                return False
            db.add(AgentRun(
                proposal_id=proposal_id,
                agent_name=COST_REVIEW_COVERAGE_AGENT,
                model_used=None,
                prompt_version=_cost_review_coverage_prompt_version(
                    current_basis,
                ),
                status=AgentRunStatus.COMPLETED,
                started_at=now,
                completed_at=now,
            ))
    return True


def get_cost_review_freshness(
    db: Session,
    proposal_id: int,
    *,
    scenario: str,
) -> dict[str, Any]:
    """Return whether completed IT cost-review evidence is still current.

    Coverage markers are the authoritative path for new runs.  A proposal that
    has never acquired a marker or invalidation may use legacy completed
    provider rows, preserving pre-upgrade proposals.  Once pricing changes,
    only a new matching coverage marker can restore readiness.
    """
    latest_invalidation_id = int(db.scalar(
        select(func.max(AgentRun.id)).where(
            AgentRun.proposal_id == proposal_id,
            AgentRun.agent_name == COST_REVIEW_INVALIDATION_AGENT,
        )
    ) or 0)
    any_coverage_id = int(db.scalar(
        select(func.max(AgentRun.id)).where(
            AgentRun.proposal_id == proposal_id,
            AgentRun.agent_name == COST_REVIEW_COVERAGE_AGENT,
        )
    ) or 0)
    current_basis = _it_cost_review_basis(db, proposal_id)
    expected_version = (
        _cost_review_coverage_prompt_version(current_basis)
        if current_basis is not None
        else ""
    )
    matching_coverage_id = int(db.scalar(
        select(func.max(AgentRun.id)).where(
            AgentRun.proposal_id == proposal_id,
            AgentRun.agent_name == COST_REVIEW_COVERAGE_AGENT,
            AgentRun.status == AgentRunStatus.COMPLETED,
            AgentRun.prompt_version == expected_version,
            AgentRun.id > latest_invalidation_id,
        )
    ) or 0)

    if matching_coverage_id:
        return {
            "verified": True,
            "legacy": False,
            "review_count": 1,
            "detail": f"Current {scenario.upper()} cost build reviewed",
        }

    # Backward compatibility is intentionally one-way: the first coverage or
    # invalidation marker opts the proposal into strict freshness semantics.
    if not any_coverage_id and not latest_invalidation_id:
        legacy_count = int(db.scalar(
            select(func.count()).select_from(AgentRun).where(
                AgentRun.proposal_id == proposal_id,
                AgentRun.agent_name.like("cost_reviewer:%"),
                AgentRun.status == AgentRunStatus.COMPLETED,
            )
        ) or 0)
        if legacy_count:
            return {
                "verified": True,
                "legacy": True,
                "review_count": legacy_count,
                "detail": f"{legacy_count} legacy reviewer pass(es) completed",
            }

    stale = bool(any_coverage_id or latest_invalidation_id)
    return {
        "verified": False,
        "legacy": False,
        "review_count": 0,
        "detail": (
            f"Cost review is stale for the selected {scenario.upper()} scenario; "
            "rerun Cost Reviewer"
            if stale
            else "Cost Reviewer hasn't completed yet"
        ),
    }


def payment_cost_basis_provenance(
    pricing_data: dict[str, Any],
) -> dict[str, str]:
    """Fingerprint the supplied cost basis, not a later global re-read."""
    raw = (pricing_data.get("our_cost_basis") or {}).copy()
    effective: dict[str, Any] = {}
    for key, default in _PAYMENT_COST_BASIS_DEFAULTS.items():
        value = raw.get(key, default)
        try:
            effective[key] = float(default if value is None else value)
        except (TypeError, ValueError):
            # Keep readiness deterministic and fail closed even when an
            # operator hand-edits malformed JSON values into the shared file.
            effective[key] = {"invalid": repr(value)}
    effective["confirmed_by_ops_finance"] = bool(
        raw.get("_confirmed_by_ops_finance")
    )
    return {
        "version": PAYMENT_COST_BASIS_PROVENANCE_VERSION,
        "sha256": _canonical_hash(effective),
    }


def current_payment_cost_basis_provenance() -> dict[str, str]:
    """Fingerprint the current operational inputs used by payment math."""
    from app.services.service_line import (
        load_payment_systems_pricing,
        payment_cost_basis_lock,
    )

    with payment_cost_basis_lock():
        return payment_cost_basis_provenance(
            load_payment_systems_pricing(),
        )


def stamp_payment_market_scan_provenance(
    scan_data: dict[str, Any],
    *,
    provenance: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Attach the provenance of the cost basis used for its profit math."""
    stamped = dict(scan_data)
    stamped[PAYMENT_COST_BASIS_PROVENANCE_KEY] = (
        provenance or current_payment_cost_basis_provenance()
    )
    return stamped


def payment_market_scan_is_current(scan_data: dict[str, Any]) -> bool:
    stored = scan_data.get(PAYMENT_COST_BASIS_PROVENANCE_KEY)
    return (
        isinstance(stored, dict)
        and stored == current_payment_cost_basis_provenance()
    )


def _payment_review_basis(db: Session, proposal: Proposal) -> dict[str, Any]:
    from app.services.service_line import (
        load_payment_systems_context,
        load_payment_systems_pricing,
        payment_cost_basis_lock,
    )

    raw_scan = proposal.payment_market_scan_json or ""
    try:
        scan = json.loads(raw_scan) if raw_scan.strip() else {}
    except json.JSONDecodeError:
        scan = {"_invalid_json": True, "raw_sha256": _canonical_hash(raw_scan)}

    sections = db.execute(
        select(ProposalSection)
        .where(
            ProposalSection.proposal_id == proposal.id,
            ProposalSection.requires_cost_analysis.is_(True),
        )
        .order_by(ProposalSection.section_order, ProposalSection.id)
    ).scalars().all()
    with payment_cost_basis_lock():
        pricing_data = load_payment_systems_pricing()
        context_data = load_payment_systems_context()
    return {
        "rfp_title": proposal.title or "",
        "rfp_agency": proposal.agency or "",
        "pricing_data": pricing_data,
        "context_data": context_data,
        "market_scan": scan,
        "selected_pricing_model": proposal.selected_pricing_model,
        "cost_sections": [
            {
                "id": section.id,
                "section_id": section.section_id,
                "section_title": section.section_title or "",
                "revision": section.current_revision_number or 0,
                "draft_markdown": section.draft_text_markdown or "",
            }
            for section in sections
            if (section.draft_text_markdown or "").strip()
        ],
    }


def build_payment_review_provenance(
    proposal_id: int,
    *,
    db: Session | None = None,
) -> dict[str, str] | None:
    """Fingerprint every persisted input the Payment Cost Reviewer reads."""
    if db is None:
        with session_scope() as owned_db:
            return build_payment_review_provenance(
                proposal_id,
                db=owned_db,
            )

    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        return None
    return {
        "version": PAYMENT_REVIEW_PROVENANCE_VERSION,
        "sha256": _canonical_hash(_payment_review_basis(db, proposal)),
    }


def payment_cost_review_is_current(
    proposal_id: int,
    review_data: dict[str, Any],
    *,
    db: Session | None = None,
) -> bool:
    stored = review_data.get(PAYMENT_REVIEW_PROVENANCE_KEY)
    current = build_payment_review_provenance(proposal_id, db=db)
    return isinstance(stored, dict) and current is not None and stored == current


__all__ = [
    "COST_REVIEW_COVERAGE_AGENT",
    "COST_REVIEW_INVALIDATION_AGENT",
    "PAYMENT_COST_BASIS_PROVENANCE_KEY",
    "PAYMENT_REVIEW_PROVENANCE_KEY",
    "build_payment_review_provenance",
    "capture_it_cost_review_basis",
    "current_payment_cost_basis_provenance",
    "get_cost_review_freshness",
    "invalidate_cost_review",
    "payment_cost_review_is_current",
    "payment_cost_basis_provenance",
    "payment_market_scan_is_current",
    "record_cost_review_coverage",
    "stamp_payment_market_scan_provenance",
]
