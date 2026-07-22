"""Seed one disposable, otherwise-ready draft for NEEDS_HUMAN browser coverage."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TITLE = "Synthetic NEEDS_HUMAN Lifecycle RFP"
PROVIDE_MARKER = "insert final transition-plan artifact name"
SIGN_MARKER = "authorized company representative signature"
REMOVE_MARKER = "confirm whether to retain the weekly legacy-reporting statement"


def _validate_environment() -> Path:
    if os.environ.get("APP_ENV", "").strip().lower() != "e2e":
        raise RuntimeError("NEEDS_HUMAN seed requires APP_ENV=e2e")
    if os.environ.get("RFP_E2E_FAKE_LLM", "") != "1":
        raise RuntimeError("NEEDS_HUMAN seed requires RFP_E2E_FAKE_LLM=1")

    raw_data_dir = os.environ.get("RFP_DATA_DIR", "").strip()
    if not raw_data_dir:
        raise RuntimeError("NEEDS_HUMAN seed requires an explicit RFP_DATA_DIR")
    data_dir = Path(raw_data_dir).resolve()
    canonical_data = (PROJECT_ROOT / "data").resolve()
    try:
        data_dir.relative_to(canonical_data)
    except ValueError:
        pass
    else:
        raise RuntimeError(f"refusing to seed canonical data: {data_dir}")

    database_url = os.environ.get("DATABASE_URL", "").replace("\\", "/")
    expected_db = (data_dir / "sqlite.db").as_posix()
    if database_url != f"sqlite:///{expected_db}":
        raise RuntimeError(
            "NEEDS_HUMAN seed DATABASE_URL must point exactly to sqlite.db "
            f"inside RFP_DATA_DIR; got {database_url!r}"
        )
    if not (data_dir / "sqlite.db").is_file():
        raise RuntimeError("NEEDS_HUMAN seed requires a migrated E2E database")

    profile_path = data_dir / "company_profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("NEEDS_HUMAN seed requires the synthetic profile") from exc
    if not str((profile.get("_meta") or {}).get("version") or "").startswith(
        "e2e-"
    ):
        raise RuntimeError("NEEDS_HUMAN seed rejected a non-E2E profile")
    return data_dir


def _cleanup() -> dict[str, int | bool]:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import Proposal
    from app.services.proposals import delete_proposal

    deleted = 0
    with SessionLocal() as db:
        proposals = db.execute(
            select(Proposal).where(Proposal.title == TITLE)
        ).scalars().all()
        for proposal in proposals:
            result = delete_proposal(db, proposal.id)
            if not result.get("deleted"):
                raise RuntimeError(
                    f"NEEDS_HUMAN seed cleanup failed for {proposal.id}: {result}"
                )
            deleted += 1
        db.commit()
    return {"deleted": deleted, "ok": True}


def _seed(data_dir: Path) -> dict[str, int]:
    from sqlalchemy import select

    from app.core.enums import AgentRunStatus, ProposalRole, ProposalStatus
    from app.db.session import SessionLocal
    from app.models import (
        AgentRun,
        PricingPackage,
        Proposal,
        ProposalSection,
        ProposalTeamMember,
        RfpPackage,
    )
    from app.services.review_coverage import (
        REVIEW_COVERAGE_AGENT,
        review_coverage_prompt_version,
    )
    from app.services.submission_commitments import evaluate_submission_readiness

    now = datetime.now(UTC)
    with SessionLocal() as db:
        existing = db.execute(
            select(Proposal).where(Proposal.title == TITLE)
        ).scalar_one_or_none()
        if existing is not None:
            section = db.execute(
                select(ProposalSection).where(
                    ProposalSection.proposal_id == existing.id,
                    ProposalSection.section_id == "SEC-NH-001",
                )
            ).scalar_one()
            return {
                "proposal_id": existing.id,
                "package_id": existing.rfp_package_id,
                "section_id": section.id,
                "initial_revision": section.current_revision_number,
            }

        package = RfpPackage(
            uploaded_by="e2e-needs-human-seed",
            uploaded_at=now,
            storage_dir="",
            notes="Synthetic package for NEEDS_HUMAN lifecycle coverage.",
        )
        db.add(package)
        db.flush()
        package_dir = data_dir / "rfp_packages" / str(package.id)
        package_dir.mkdir(parents=True, exist_ok=True)
        package.storage_dir = str(package_dir)

        proposal = Proposal(
            rfp_package_id=package.id,
            title=TITLE,
            agency="E2E Department of Human Review",
            naics="541512",
            due_date=date(2032, 7, 31),
            role=ProposalRole.PRIME,
            status=ProposalStatus.DRAFT_READY,
            notes="SYNTHETIC E2E ONLY",
            service_line="it_services",
            proposed_scenario="MEDIUM",
            team_approved_at=now,
        )
        db.add(proposal)
        db.flush()

        section = ProposalSection(
            proposal_id=proposal.id,
            section_id="SEC-NH-001",
            section_title="Transition Commitments and Authorization",
            section_order=1,
            section_brief=(
                "Name the transition deliverable, apply the authorized "
                "signature, and remove obsolete optional language."
            ),
            page_limit=3,
            word_limit=800,
            requires_cost_analysis=False,
            excluded_from_draft=False,
            draft_text_markdown=(
                "## Transition Commitments and Authorization\n\n"
                "Our transition package will include the "
                f"[NEEDS_HUMAN: {PROVIDE_MARKER}] for evaluator review.\n\n"
                "Authorized representative: "
                f"[NEEDS_HUMAN: {SIGN_MARKER}]\n\n"
                "Optional legacy reporting note: "
                f"[NEEDS_HUMAN: {REMOVE_MARKER}]"
            ),
            current_revision_number=1,
            compliance_items_addressed_json=[],
            citations_json=[],
            needs_human_placeholders_json=[
                {
                    "marker_text": PROVIDE_MARKER,
                    "description": (
                        "Name the final transition-plan artifact promised "
                        "to evaluators."
                    ),
                    "category": "schedule_commitment",
                },
                {
                    "marker_text": SIGN_MARKER,
                    "description": (
                        "Authorized company representative signature for "
                        "the final proposal."
                    ),
                    "category": "signature",
                },
                {
                    "marker_text": REMOVE_MARKER,
                    "description": (
                        "Confirm whether the optional weekly legacy-reporting "
                        "statement should remain."
                    ),
                    "category": "other",
                },
            ],
            shortfall_mitigations_applied_json=[],
            compliance_drift_pending=False,
        )
        db.add(section)
        db.flush()

        db.add(
            ProposalTeamMember(
                proposal_id=proposal.id,
                role_name="Program Manager",
                person_kind="named",
                assigned_person="Alex Morgan",
                labor_category="Project Manager I",
                wage_band="150k",
                time_allocation_pct=100,
                experience_years=12,
                bio_summary="Synthetic E2E program manager.",
                phases_active_json=["Delivery"],
                display_order=0,
            )
        )

        for scenario, price in (
            ("LOW", 900_000),
            ("MEDIUM", 1_000_000),
            ("HIGH", 1_100_000),
        ):
            db.add(
                PricingPackage(
                    proposal_id=proposal.id,
                    scenario=scenario,
                    loaded_labor_cost=750_000,
                    odcs_json=[],
                    subcontractor_costs=0,
                    indirect_costs_json={},
                    total_proposed_price=price,
                    pnl_projection_json={},
                    phase_breakdown_json=[],
                    bid_recommendation="bid",
                    recommendation_rationale="Synthetic readiness fixture.",
                )
            )

        for agent_name in (
            "cost_reviewer:e2e-seed",
            "reviewer_a",
            "reviewer_b",
        ):
            db.add(
                AgentRun(
                    proposal_id=proposal.id,
                    agent_name=agent_name,
                    model_used="e2e-fixture",
                    prompt_version="e2e-v1",
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0,
                    started_at=now,
                    completed_at=now,
                    status=AgentRunStatus.COMPLETED,
                )
            )
        db.add(
            AgentRun(
                proposal_id=proposal.id,
                agent_name=REVIEW_COVERAGE_AGENT,
                model_used=None,
                prompt_version=review_coverage_prompt_version(
                    section.id, section.current_revision_number,
                ),
                input_tokens=0,
                output_tokens=0,
                cost_usd=0,
                started_at=now,
                completed_at=now,
                status=AgentRunStatus.COMPLETED,
            )
        )

        db.commit()
        proposal_id = proposal.id
        package_id = package.id
        section_id = section.id

    readiness = evaluate_submission_readiness(proposal_id)
    expected_blocker = (
        "All NEEDS_HUMAN placeholders resolved: "
        "3 placeholder(s) still pending"
    )
    if readiness["ready"] or readiness["blockers"] != [expected_blocker]:
        raise RuntimeError(
            "NEEDS_HUMAN seed is not otherwise submission-ready: "
            f"{readiness['blockers']!r}"
        )
    system_checks = readiness["snapshot"]["system_checks"]
    verified = sum(1 for item in system_checks if item["verified"])
    if (verified, len(system_checks)) != (8, 9):
        raise RuntimeError(
            "unexpected seeded readiness total: "
            f"{verified}/{len(system_checks)}"
        )

    return {
        "proposal_id": proposal_id,
        "package_id": package_id,
        "section_id": section_id,
        "initial_revision": 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    data_dir = _validate_environment()
    payload = _cleanup() if args.cleanup else _seed(data_dir)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
