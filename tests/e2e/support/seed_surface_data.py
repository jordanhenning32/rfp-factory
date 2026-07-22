"""Seed a representative proposal graph in the disposable E2E workspace.

This entrypoint is intentionally separate from the pytest process. Importing
``app`` binds module-level settings and database engines, so the browser suite
invokes this script with the exact environment used by the already-running
E2E server. Guardrails reject every non-E2E or canonical data directory.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

IT_TITLE = "Synthetic IT Modernization RFP"
PAYMENT_TITLE = "Synthetic Payment Processing RFP"


def _validate_environment() -> Path:
    if os.environ.get("APP_ENV", "").strip().lower() != "e2e":
        raise RuntimeError("surface seed requires APP_ENV=e2e")
    if os.environ.get("RFP_E2E_FAKE_LLM", "") != "1":
        raise RuntimeError("surface seed requires RFP_E2E_FAKE_LLM=1")

    raw_data_dir = os.environ.get("RFP_DATA_DIR", "").strip()
    if not raw_data_dir:
        raise RuntimeError("surface seed requires an explicit RFP_DATA_DIR")
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
            "surface seed DATABASE_URL must point exactly to sqlite.db inside "
            f"RFP_DATA_DIR; got {database_url!r}"
        )
    if not (data_dir / "sqlite.db").is_file():
        raise RuntimeError("surface seed requires an already-migrated E2E database")

    profile_path = data_dir / "company_profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("surface seed requires the synthetic E2E profile") from exc
    version = str((profile.get("_meta") or {}).get("version") or "")
    if not version.startswith("e2e-"):
        raise RuntimeError(
            f"surface seed rejected non-E2E profile version {version!r}"
        )
    return data_dir


def _json(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _write_synthetic_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(data_dir: Path) -> dict[str, int]:
    from sqlalchemy import select

    from app.core.enums import (
        AgentRunStatus,
        ComplianceStatus,
        FindingCategory,
        FindingSeverity,
        GapSeverity,
        KbDocumentClass,
        KbDocumentStatus,
        ProposalOutcomeStatus,
        ProposalRole,
        ProposalStatus,
        RequirementCategory,
        RequirementType,
        ReviewerAgent,
        RfpDocumentType,
    )
    from app.db.session import SessionLocal
    from app.models import (
        AgentRun,
        AmendmentRun,
        ComplianceMatrixItem,
        CostReviewFinding,
        GapAnalysis,
        KnowledgeBaseChunk,
        KnowledgeBaseDocument,
        LearnedRule,
        MarketScan,
        MarketScanComparableAward,
        MarketScanCompetitor,
        PricingPackage,
        PricingPackageLine,
        ProfileSuggestion,
        Proposal,
        ProposalOutcome,
        ProposalSection,
        ProposalTeamMember,
        ReviewerFinding,
        RfpPackage,
        RfpPackageDocument,
        SubmissionCommitment,
    )

    now = datetime.now(UTC)
    with SessionLocal() as db:
        existing = db.execute(select(Proposal).order_by(Proposal.id)).scalars().all()
        if existing:
            by_title = {p.title: p.id for p in existing}
            if set(by_title) == {IT_TITLE, PAYMENT_TITLE}:
                return {
                    "it_proposal_id": by_title[IT_TITLE],
                    "payment_proposal_id": by_title[PAYMENT_TITLE],
                }
            raise RuntimeError(
                "surface seed refuses a non-empty database that it did not create"
            )

        # Physical package sources let the Amendments and document surfaces
        # exercise real paths without copying anything from the repository.
        it_pkg_dir = data_dir / "rfp_packages" / "synthetic-it"
        payment_pkg_dir = data_dir / "rfp_packages" / "synthetic-payment"
        it_source = it_pkg_dir / "synthetic_it_rfp.txt"
        amendment_source = it_pkg_dir / "amendment_0001.txt"
        payment_source = payment_pkg_dir / "synthetic_payment_rfp.txt"
        _write_synthetic_file(
            it_source,
            "Synthetic solicitation: modernize the agency platform and submit all forms.",
        )
        _write_synthetic_file(
            amendment_source,
            "Synthetic amendment 0001: clarify transition reporting cadence.",
        )
        _write_synthetic_file(
            payment_source,
            "Synthetic solicitation: provide secure payment processing services.",
        )

        it_pkg = RfpPackage(
            uploaded_by="e2e-seed",
            uploaded_at=now,
            storage_dir=str(it_pkg_dir),
            notes="Synthetic package for real-browser product coverage.",
        )
        db.add(it_pkg)
        db.flush()
        it_doc = RfpPackageDocument(
            rfp_package_id=it_pkg.id,
            filename=it_source.name,
            storage_path=str(it_source),
            document_type=RfpDocumentType.MAIN_SOLICITATION,
            document_role="original",
            page_count=8,
            extracted_text_md=it_source.read_text(encoding="utf-8"),
            structure_json={"sections": ["L", "M"]},
        )
        amendment_doc = RfpPackageDocument(
            rfp_package_id=it_pkg.id,
            filename=amendment_source.name,
            storage_path=str(amendment_source),
            document_type=RfpDocumentType.AMENDMENT,
            document_role="amendment",
            sequence_number=1,
            page_count=2,
            extracted_text_md=amendment_source.read_text(encoding="utf-8"),
            structure_json={"sections": ["clarification"]},
        )
        db.add_all([it_doc, amendment_doc])

        evaluation = {
            "evaluation_method": "best_value",
            "factors": [
                {
                    "factor_id": "F1",
                    "factor_name": "Technical Approach",
                    "weight_pct": 60,
                    "weight_descriptive": "Most important",
                    "scoring_scale": "Outstanding to Unacceptable",
                    "evidence_required": "Traceable architecture and transition proof.",
                    "subfactors": [
                        {"name": "Transition", "weight_pct": 25, "notes": "Low risk"}
                    ],
                },
                {
                    "factor_id": "F2",
                    "factor_name": "Management and Staffing",
                    "weight_pct": 40,
                    "weight_descriptive": "Second most important",
                    "scoring_scale": "Outstanding to Unacceptable",
                    "evidence_required": "Named accountable delivery team.",
                    "subfactors": [],
                },
            ],
            "section_l_to_m_map": {
                "REQ-001": ["F1"],
                "REQ-003": ["F2"],
            },
            "trade_off_language": "Technical merit may justify a price premium.",
            "lowest_price_clause": None,
            "extraction_notes": "Synthetic Section M extraction.",
        }
        it = Proposal(
            rfp_package_id=it_pkg.id,
            title=IT_TITLE,
            agency="Synthetic Digital Services Agency",
            naics="541512",
            due_date=date(2030, 9, 30),
            role=ProposalRole.PRIME,
            status=ProposalStatus.DRAFT_READY,
            notes="Representative IT-services graph for isolated E2E coverage.",
            cots_orientation=True,
            team_approved_at=now,
            teaming_framing="open",
            build_framing="self_perform_first",
            proposed_scenario="MEDIUM",
            service_line="it_services",
            evaluation_criteria_json=_json(evaluation),
            timeline_json=_json(
                {
                    "anchor_date": "2030-10-15",
                    "phases": [
                        {
                            "id": "phase-seeded-discovery",
                            "phase_name": "Seeded Discovery",
                            "start_offset": 0,
                            "duration": 30,
                            "deliverable": "Validated modernization roadmap",
                            "owner": "Program Manager",
                            "color": "#1F3A5F",
                            "order": 0,
                        }
                    ],
                }
            ),
        )
        db.add(it)
        db.flush()

        sec_technical = ProposalSection(
            proposal_id=it.id,
            section_id="SEC-001",
            section_title="Technical Approach",
            section_order=1,
            section_brief="Explain a low-risk, evaluator-mapped modernization approach.",
            page_limit=8,
            word_limit=2400,
            draft_text_markdown=(
                "## Technical Approach\n\n"
                "Our synthetic modernization approach uses measurable transition gates. "
                "The unique browser-test sentence proves the latest persisted draft was exported.\n\n"
                "| Gate | Evidence |\n|---|---|\n| Discovery | Approved roadmap |\n"
            ),
            current_revision_number=2,
            compliance_items_addressed_json=["REQ-001"],
            citations_json=[
                {
                    "claim": "Transition gates reduce delivery risk.",
                    "source_kb_doc": "synthetic_capability.txt",
                    "source_section": "Delivery",
                    "confidence": "HIGH",
                }
            ],
            needs_human_placeholders_json=[],
            shortfall_mitigations_applied_json=[],
        )
        sec_management = ProposalSection(
            proposal_id=it.id,
            section_id="SEC-002",
            section_title="Management and Staffing",
            section_order=2,
            section_brief="Name accountable leaders and reporting cadence.",
            page_limit=5,
            draft_text_markdown=(
                "## Management and Staffing\n\n"
                "Jordan Example leads a compact delivery team with weekly buyer reporting."
            ),
            current_revision_number=1,
            compliance_items_addressed_json=["REQ-003"],
            citations_json=[
                {
                    "claim": "Named delivery leadership.",
                    "source_kb_doc": "synthetic_capability.txt",
                    "source_section": "Personnel",
                    "confidence": "HIGH",
                }
            ],
            needs_human_placeholders_json=[],
            shortfall_mitigations_applied_json=[],
        )
        sec_cost = ProposalSection(
            proposal_id=it.id,
            section_id="SEC-003",
            section_title="Cost Proposal",
            section_order=3,
            section_brief="Present a transparent, defensible labor build.",
            requires_cost_analysis=True,
            draft_text_markdown=(
                "## Cost Proposal\n\nThe target scenario is $525,000 with explicit assumptions."
            ),
            current_revision_number=1,
            compliance_items_addressed_json=["REQ-004"],
            citations_json=[],
            needs_human_placeholders_json=[],
            shortfall_mitigations_applied_json=[],
        )
        db.add_all([sec_technical, sec_management, sec_cost])
        db.flush()

        req_technical = ComplianceMatrixItem(
            proposal_id=it.id,
            requirement_id="REQ-001",
            requirement_text="The offeror shall provide a phased modernization approach.",
            source_doc=it_source.name,
            source_section="L.3.1",
            source_page=3,
            requirement_type=RequirementType.SHALL,
            category=RequirementCategory.TECHNICAL,
            weight=0.60,
            compliance_status=ComplianceStatus.DRAFTED,
            linked_response_section_id=sec_technical.id,
        )
        req_form = ComplianceMatrixItem(
            proposal_id=it.id,
            requirement_id="REQ-002",
            requirement_text="Submit the signed synthetic representations form.",
            source_doc=it_source.name,
            source_section="L.8",
            source_page=7,
            requirement_type=RequirementType.MANDATORY_FORM,
            category=RequirementCategory.ADMINISTRATIVE,
            compliance_status=ComplianceStatus.TO_BE_DRAFTED,
            submission_obtained=False,
            submission_notes="Obtain before submission.",
        )
        req_unassigned = ComplianceMatrixItem(
            proposal_id=it.id,
            requirement_id="REQ-003",
            requirement_text="The offeror must identify the accountable program manager.",
            source_doc=it_source.name,
            source_section="M.2",
            source_page=8,
            requirement_type=RequirementType.EVALUATION_CRITERION,
            category=RequirementCategory.PERSONNEL,
            weight=0.40,
            compliance_status=ComplianceStatus.DRAFTED,
            linked_response_section_id=None,
        )
        req_cost = ComplianceMatrixItem(
            proposal_id=it.id,
            requirement_id="REQ-004",
            requirement_text="The offeror shall provide a complete price narrative.",
            source_doc=it_source.name,
            source_section="L.9",
            source_page=8,
            requirement_type=RequirementType.SHALL,
            category=RequirementCategory.PRICING,
            compliance_status=ComplianceStatus.DRAFTED,
            linked_response_section_id=sec_cost.id,
            amendment_origin=amendment_source.name,
        )
        db.add_all([req_technical, req_form, req_unassigned, req_cost])
        db.flush()

        gap = GapAnalysis(
            proposal_id=it.id,
            requirement_id_fk=req_technical.id,
            gap_id="GAP-001",
            gap_severity=GapSeverity.TECHNICAL,
            gap_description="Synthetic transition proof is not yet specific enough.",
            current_state="A reusable delivery playbook exists, but this agency mapping is new.",
            mitigation_options_json=[
                {
                    "approach": "Map the reusable playbook to agency gates",
                    "proposal_language_draft": "We will map each transition gate to buyer acceptance evidence.",
                    "honesty_check": "Uses existing process without claiming prior agency delivery.",
                    "additional_action_required": "Validate gates during kickoff.",
                },
                {
                    "approach": "Team with a transition specialist",
                    "proposal_language_draft": "A specialist will validate the transition sequence.",
                    "honesty_check": "Requires a named partner before submission.",
                    "additional_action_required": "Confirm partner availability.",
                    "partner_suggestions": [
                        {"name": "Synthetic Transition Partners", "why": "Relevant transition depth"}
                    ],
                },
            ],
            recommended_mitigation_index=0,
            selected_mitigation_index=None,
            resolved=False,
        )
        db.add(gap)

        team_member = ProposalTeamMember(
            proposal_id=it.id,
            role_name="Program Manager",
            person_kind="named",
            assigned_person="Jordan Example",
            labor_category="Program Manager",
            wage_band="145k",
            time_allocation_pct=75,
            experience_years=12,
            bio_summary="Leads phased public-sector modernization programs.",
            phases_active_json=["Discovery", "Delivery"],
            display_order=1,
        )
        db.add(team_member)

        market_scan = MarketScan(
            proposal_id=it.id,
            market_band_low_usd=450000,
            market_band_mid_usd=525000,
            market_band_high_usd=625000,
            methodology="Synthetic three-award normalized market band.",
        )
        db.add(market_scan)
        db.flush()
        db.add(
            MarketScanComparableAward(
                market_scan_id=market_scan.id,
                award_title="Synthetic Cloud Modernization Award",
                award_value_usd=540000,
                period_of_performance_months=12,
                awardee_name="Example Integrator",
                customer_agency="Synthetic Services Bureau",
                source_url="https://example.invalid/synthetic-award",
                relevance_score=0.91,
                notes="Synthetic public-award analogue for isolated testing.",
                confirmed_by=["gemini", "claude"],
                needs_review=False,
            )
        )
        db.add(
            MarketScanCompetitor(
                market_scan_id=market_scan.id,
                competitor_name="Example Integrator",
                likelihood_to_bid="high",
                estimated_rate_low_usd=145,
                estimated_rate_high_usd=190,
                rate_estimation_basis="Synthetic normalized award rate.",
                source_urls=["https://example.invalid/synthetic-competitor"],
                notes="Synthetic competitor only.",
                confirmed_by=["gemini", "claude"],
                needs_review=False,
            )
        )

        scenario_values = {
            "LOW": (475000, 380000, 0.20, "below"),
            "MEDIUM": (525000, 393750, 0.25, "in_band"),
            "HIGH": (600000, 420000, 0.30, "in_band"),
        }
        packages: dict[str, PricingPackage] = {}
        for scenario, (price, labor, margin, position) in scenario_values.items():
            package = PricingPackage(
                proposal_id=it.id,
                scenario=scenario,
                market_scan_id=market_scan.id,
                loaded_labor_cost=labor,
                odcs_json=[
                    {"item": "Secure test environment", "amount": 12000, "justification": "Validation"}
                ],
                subcontractor_costs=25000,
                indirect_costs_json={
                    "ga_hourly_addon_usd": 8.0,
                    "ga_total_usd": 24000,
                    "contingency_hours_usd": 12000,
                    "contingency_cost_usd": 12000,
                    "profit_pct": margin,
                    "profit_usd": price - labor - 61000,
                    "total_subtotal_cost_usd": labor + 61000,
                },
                total_proposed_price=price,
                pnl_projection_json={
                    "revenue": price,
                    "cogs": labor + 61000,
                    "gross_margin": price - labor - 61000,
                    "gross_margin_pct": margin,
                    "blended_hourly_rate": price / 3000,
                    "break_even_hours": 2600,
                    "sensitivity": [],
                },
                phase_breakdown_json=[
                    {
                        "name": "Discovery",
                        "description": "Validate scope and architecture.",
                        "start_month": 0,
                        "duration_months": 1,
                        "labor_allocations": [],
                        "phase_price_usd": price * 0.2,
                    },
                    {
                        "name": "Implementation",
                        "description": "Configure, migrate, and validate.",
                        "start_month": 1,
                        "duration_months": 8,
                        "labor_allocations": [],
                        "phase_price_usd": price * 0.8,
                    },
                ],
                vs_market_position=position,
                bid_recommendation="bid",
                recommendation_rationale=f"Synthetic {scenario.lower()} scenario rationale.",
            )
            db.add(package)
            db.flush()
            db.add(
                PricingPackageLine(
                    pricing_package_id=package.id,
                    labor_category="Program Manager",
                    wage_band="145k",
                    coverage_level="low" if scenario == "LOW" else "high",
                    hours=1000,
                    loaded_hourly_rate_usd=125,
                    loaded_cost_usd=125000,
                    ga_allocation_usd=8000,
                    proposed_billing_rate_usd=175,
                    billed_total_usd=175000,
                    profit_per_hour_usd=42,
                    rationale="One accountable PM across the synthetic period of performance.",
                )
            )
            packages[scenario] = package

        cost_finding = CostReviewFinding(
            pricing_package_id=packages["MEDIUM"].id,
            finding_text="Security validation hours may be understated for the synthetic scope.",
            severity=FindingSeverity.MAJOR,
            category="hours",
            alternative_scenarios_json=[
                {
                    "label": "Add validation sprint",
                    "total_price": 548000,
                    "rationale": "Adds focused security testing.",
                    "margin_delta": -0.02,
                }
            ],
            recommended_change="Add 120 security-validation hours and explain the gate.",
            user_action="pending",
            auto_actioned=False,
        )
        db.add(cost_finding)

        reviewer_finding = ReviewerFinding(
            proposal_section_id=sec_technical.id,
            reviewer_agent=ReviewerAgent.A_COMPLIANCE_RISK,
            pass_number=1,
            severity=FindingSeverity.MINOR,
            category=FindingCategory.UNCITED_CLAIM,
            finding_text="Clarify how transition-gate evidence is verified.",
            suggested_fix="Name the buyer acceptance artifact for every gate.",
            accepted_at=now,
        )
        db.add(reviewer_finding)
        db.flush()
        db.add(
            LearnedRule(
                kind="writer_avoid",
                rule_text="Tie each transition claim to a named acceptance artifact.",
                source_finding_id=reviewer_finding.id,
                source_action="accept",
                source_category="uncited_claim",
                source_severity="MINOR",
                status="draft",
                hits=0,
            )
        )
        db.add(
            SubmissionCommitment(
                proposal_id=it.id,
                description="Provide the synthetic transition-gate evidence matrix.",
                source="needs_human_apply",
                source_section_id=sec_technical.id,
                obtained=False,
                notes="Export from the delivery workbook before submission.",
            )
        )
        db.add(
            AmendmentRun(
                proposal_id=it.id,
                document_id=amendment_doc.id,
                started_at=now,
                completed_at=now,
                status="completed",
                report_json=_json(
                    {
                        "added": 1,
                        "modified": 1,
                        "removed": 0,
                        "sections_marked_stale": 0,
                        "summary": "Synthetic amendment applied successfully.",
                    }
                ),
            )
        )
        for agent_name, cost in [
            ("compliance_matrix", 0.0412),
            ("writer", 0.1234),
            ("reviewer_a", 0.0315),
            ("cost_analyst", 0.0520),
            ("final_polish_applier", 0.0080),
        ]:
            db.add(
                AgentRun(
                    proposal_id=it.id,
                    agent_name=agent_name,
                    model_used="e2e-fixture-model",
                    prompt_version="e2e-v1",
                    input_tokens=1200,
                    output_tokens=340,
                    cost_usd=cost,
                    started_at=now,
                    completed_at=now,
                    status=AgentRunStatus.COMPLETED,
                )
            )

        # Payment-systems proposal exercises the separate scan and JSON-backed
        # Cost Review path plus the terminal-status Outcome panel.
        payment_pkg = RfpPackage(
            uploaded_by="e2e-seed",
            uploaded_at=now,
            storage_dir=str(payment_pkg_dir),
            notes="Synthetic payment package.",
        )
        db.add(payment_pkg)
        db.flush()
        db.add(
            RfpPackageDocument(
                rfp_package_id=payment_pkg.id,
                filename=payment_source.name,
                storage_path=str(payment_source),
                document_type=RfpDocumentType.MAIN_SOLICITATION,
                document_role="original",
                page_count=5,
                extracted_text_md=payment_source.read_text(encoding="utf-8"),
                structure_json={"sections": ["payments"]},
            )
        )
        payment_scan = {
            "pricing_structure": {
                "pricing_model": "interchange_plus",
                "pricing_model_rationale": "Transparent pass-through pricing fits the synthetic buyer's volume.",
                "proposed_credit_card_markup_bps": 24,
                "median_market_credit_card_markup_bps": 30,
                "proposed_per_txn_fee_usd": 0.09,
                "median_market_per_txn_fee_usd": 0.12,
                "proposed_ach_fee_usd": 0.35,
                "median_market_ach_fee_usd": 0.45,
                "proposed_monthly_fee_usd": 250,
                "median_market_monthly_fee_usd": 350,
                "rate_positioning": "below_market",
                "other_fees_recommended": [
                    {"name": "Chargeback handling", "amount_usd": 15.0, "notes": "Only when incurred"}
                ],
            },
            "volume_estimate": {
                "annual_processed_volume_low_usd": 8000000,
                "annual_processed_volume_midpoint_usd": 10000000,
                "annual_processed_volume_high_usd": 13000000,
                "estimated_transaction_count_annual": 125000,
                "average_transaction_size_usd": 80,
                "confidence": "medium",
                "estimation_basis": "Synthetic transaction history normalized to one year.",
            },
            "profit_math": {
                "annual_processor_revenue_midpoint_usd": 56000,
                "annual_internal_costs_usd": 31000,
                "annual_net_profit_midpoint_usd": 25000,
                "profit_margin_pct_at_midpoint": 0.446,
                "cost_basis_assumptions": ["Synthetic gateway and support costs only."],
                "computation_notes": "Markup plus transaction and monthly fees less internal costs.",
            },
            "comparable_awards": [
                {
                    "processor_name": "SyntheticPay",
                    "customer_name": "Example Municipality",
                    "award_year": 2028,
                    "pricing_model": "interchange_plus",
                    "disclosed_credit_card_rate_text": "Interchange plus 31 bps and $0.10",
                    "annual_volume_estimate_usd": 9000000,
                    "contract_term_years": 3,
                    "source_url": "https://example.invalid/payment-award",
                    "confirmed_by": ["gemini", "claude"],
                    "needs_review": False,
                }
            ],
            "competitor_processors": [
                {
                    "name": "SyntheticPay",
                    "market_position": "mid_market",
                    "likelihood_to_bid": "high",
                    "typical_pricing_summary": "Interchange plus 30-35 bps and $0.10-$0.15 per transaction.",
                    "source_urls": ["https://example.invalid/payment-competitor"],
                    "confirmed_by": ["gemini", "claude"],
                    "needs_review": False,
                    "notes": "Synthetic competitor only.",
                }
            ],
            "insufficient_data_warning": False,
            "citations": [
                {"title": "Synthetic processor disclosure", "uri": "https://example.invalid/payment-citation"}
            ],
        }
        payment_review = {
            "overall_assessment": "Competitive synthetic posture; clarify the chargeback disclosure.",
            "bid_ready": False,
            "findings": [
                {
                    "finding_id": "PAY-COST-001",
                    "severity": "MAJOR",
                    "category": "FEE_DISCLOSURE",
                    "section_id": "PAY-SEC-002",
                    "section_title": "Payment Fee Narrative",
                    "finding_text": "The chargeback fee needs an explicit when-incurred qualifier.",
                    "cited_quote": "$15 chargeback handling fee",
                    "suggested_fix": "State that the $15 chargeback fee applies only when incurred.",
                    "user_action": "pending",
                    "user_note": None,
                }
            ],
        }
        payment = Proposal(
            rfp_package_id=payment_pkg.id,
            title=PAYMENT_TITLE,
            agency="Synthetic Treasury Office",
            naics="522320",
            due_date=date(2030, 10, 15),
            role=ProposalRole.PRIME,
            status=ProposalStatus.SUBMITTED,
            notes="Representative payment-systems graph for isolated E2E coverage.",
            submitted_at=now,
            service_line="payment_systems",
            payment_market_scan_json=_json(payment_scan),
            selected_pricing_model=None,
            payment_cost_review_findings_json=_json(payment_review),
            evaluation_criteria_json=_json(
                {
                    "evaluation_method": "lpta",
                    "factors": [
                        {
                            "factor_id": "PF1",
                            "factor_name": "Payment Security and Fees",
                            "weight_pct": 100,
                            "subfactors": [],
                        }
                    ],
                    "section_l_to_m_map": {"PAY-REQ-001": ["PF1"]},
                }
            ),
        )
        db.add(payment)
        db.flush()
        payment_section = ProposalSection(
            proposal_id=payment.id,
            section_id="PAY-SEC-002",
            section_title="Payment Fee Narrative",
            section_order=1,
            section_brief="Explain transparent fee mechanics and controls.",
            requires_cost_analysis=True,
            draft_text_markdown=(
                "## Payment Fee Narrative\n\nWe propose interchange-plus pricing with a $15 "
                "chargeback handling fee and transparent monthly reporting."
            ),
            current_revision_number=1,
            compliance_items_addressed_json=["PAY-REQ-001"],
            citations_json=[],
            needs_human_placeholders_json=[],
            shortfall_mitigations_applied_json=[],
        )
        db.add(payment_section)
        db.flush()
        db.add(
            ComplianceMatrixItem(
                proposal_id=payment.id,
                requirement_id="PAY-REQ-001",
                requirement_text="Provide PCI-aligned payment processing and a complete fee schedule.",
                source_doc=payment_source.name,
                source_section="3.2",
                source_page=2,
                requirement_type=RequirementType.SHALL,
                category=RequirementCategory.PRICING,
                compliance_status=ComplianceStatus.DRAFTED,
                linked_response_section_id=payment_section.id,
            )
        )
        db.add(
            ReviewerFinding(
                proposal_section_id=payment_section.id,
                reviewer_agent=ReviewerAgent.A_COMPLIANCE_RISK,
                pass_number=1,
                severity=FindingSeverity.MINOR,
                category=FindingCategory.UNCITED_CLAIM,
                finding_text=(
                    "Confirm the payment-fee qualifier against the final schedule."
                ),
                suggested_fix=(
                    "Tie the qualifier to the named fee-schedule attachment."
                ),
            )
        )
        db.add(
            ProposalOutcome(
                proposal_id=payment.id,
                submitted_at=now,
                outcome=ProposalOutcomeStatus.LOST,
                decided_at=now,
                our_proposed_price_usd=56000,
                awarded_price_usd=52000,
                awarded_to="Original Synthetic Awardee",
                debrief_received=True,
                our_total_score=84,
                winning_total_score=91,
                debrief_notes="Synthetic debrief for outcome-surface coverage.",
                factor_scores_json=[
                    {
                        "factor_id": "PF1",
                        "factor_name": "Payment Security and Fees",
                        "our_score": 84,
                        "winning_score": 91,
                        "max_score": 100,
                        "notes": "Fee disclosure clarity drove the difference.",
                    }
                ],
            )
        )
        db.add(
            AgentRun(
                proposal_id=payment.id,
                agent_name="payment_market_researcher",
                model_used="e2e-fixture-model",
                prompt_version="e2e-v1",
                input_tokens=900,
                output_tokens=250,
                cost_usd=0.064,
                started_at=now,
                completed_at=now,
                status=AgentRunStatus.COMPLETED,
            )
        )

        # KB / Configuration surfaces.
        kb_path = data_dir / "kb_documents" / "synthetic_capability.txt"
        _write_synthetic_file(
            kb_path,
            "Synthetic capability evidence: phased delivery, named accountability, and measurable gates.",
        )
        kb_doc = KnowledgeBaseDocument(
            filename=kb_path.name,
            storage_path=str(kb_path),
            document_class=KbDocumentClass.CORPORATE,
            tags_json=["synthetic", "modernization"],
            status=KbDocumentStatus.ACTIVE,
            extracted_text_md=kb_path.read_text(encoding="utf-8"),
            metadata_json={"source": "isolated-e2e-seed"},
        )
        db.add(kb_doc)
        db.flush()
        db.add(
            KnowledgeBaseChunk(
                document_id=kb_doc.id,
                chunk_index=0,
                chunk_text="Phased delivery uses named acceptance gates.",
                section_label="Delivery",
                page=1,
                embedding_model=None,
                embedding_bytes=None,
            )
        )
        suggestion = ProfileSuggestion(
            kb_document_id=kb_doc.id,
            operation="append",
            section="certifications",
            match_key=None,
            proposed_value_json="Synthetic Delivery Certification",
            current_value_json=[],
            summary="Add the synthetic delivery certification.",
            rationale="Seeded pending suggestion for Config triage coverage.",
            status="pending",
        )
        db.add(suggestion)

        db.commit()
        result = {
            "it_proposal_id": it.id,
            "payment_proposal_id": payment.id,
            "it_requirement_id": req_form.id,
            "gap_id": gap.id,
            "team_member_id": team_member.id,
            "cost_finding_id": cost_finding.id,
            "reviewer_finding_id": reviewer_finding.id,
            "profile_suggestion_id": suggestion.id,
        }

    # Generate strategy through the same deterministic product service used by
    # the Win Strategy tab. This both ensures the seeded artifact schema tracks
    # the implementation and gives every subpanel representative content.
    from app.services.win_strategy import generate_all_win_strategy

    generated = generate_all_win_strategy(result["it_proposal_id"])
    if set(generated) != {
        "evaluator_scorecard",
        "win_themes",
        "past_performance_matches",
        "price_to_win",
        "red_team_findings",
        "graphics_tables",
    }:
        raise RuntimeError("win-strategy seed did not produce every artifact")

    decisions = {
        "_meta": {"version": "e2e-1.0.0"},
        "decisions": [
            {
                "id": "DEC-001",
                "topic": "Synthetic transition specialist",
                "decision": "Use a specialist only when the buyer requires prior agency transition proof.",
                "applies_to_gaps_like": "Transition experience and agency-specific proof gaps.",
                "established_on": date.today().isoformat(),
                "source_proposal_id": result["it_proposal_id"],
                "source_gap_id": "GAP-001",
            }
        ],
    }
    (data_dir / "decisions.json").write_text(
        _json(decisions) + "\n", encoding="utf-8"
    )
    return result


def _cleanup(data_dir: Path) -> dict[str, int]:
    """Remove only the records and files owned by this synthetic seed.

    The E2E application server is session-scoped, so a function-scoped browser
    case must return the shared workspace to its pre-seed state for subsequent
    tests. Every database target is selected by the two unique seed titles;
    every filesystem target is an explicit direct child of the disposable
    data root validated above.
    """
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import AgentRun, KnowledgeBaseDocument, LearnedRule, Proposal, RfpPackage

    removed = {"proposals": 0, "kb_documents": 0, "directories": 0}
    proposal_ids: list[int] = []
    with SessionLocal() as db:
        proposals = (
            db.execute(
                select(Proposal).where(Proposal.title.in_([IT_TITLE, PAYMENT_TITLE]))
            )
            .scalars()
            .all()
        )
        for proposal in proposals:
            proposal_ids.append(proposal.id)
            package_id = proposal.rfp_package_id
            # AgentRun is not an ORM child relationship; delete explicitly as
            # belt-and-suspenders even though its FK also cascades.
            db.query(AgentRun).filter(AgentRun.proposal_id == proposal.id).delete(
                synchronize_session=False
            )
            db.delete(proposal)
            db.flush()
            package = db.get(RfpPackage, package_id)
            if package is not None:
                db.delete(package)
                db.flush()
            removed["proposals"] += 1

        db.query(LearnedRule).filter(
            LearnedRule.rule_text
            == "Tie each transition claim to a named acceptance artifact."
        ).delete(synchronize_session=False)

        kb_docs = (
            db.execute(
                select(KnowledgeBaseDocument).where(
                    KnowledgeBaseDocument.filename == "synthetic_capability.txt"
                )
            )
            .scalars()
            .all()
        )
        for document in kb_docs:
            db.delete(document)
            removed["kb_documents"] += 1
        db.commit()

    packages_root = (data_dir / "rfp_packages").resolve()
    for name in ("synthetic-it", "synthetic-payment"):
        target = (packages_root / name).resolve()
        if target.parent != packages_root:
            raise RuntimeError(f"unsafe synthetic cleanup target: {target}")
        if target.is_dir():
            shutil.rmtree(target)
            removed["directories"] += 1

    kb_file = (data_dir / "kb_documents" / "synthetic_capability.txt").resolve()
    if kb_file.parent != (data_dir / "kb_documents").resolve():
        raise RuntimeError(f"unsafe synthetic KB cleanup target: {kb_file}")
    kb_file.unlink(missing_ok=True)

    decisions_path = data_dir / "decisions.json"
    try:
        decisions_doc = json.loads(decisions_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        decisions_doc = {"_meta": {"version": "e2e-1.0.0"}, "decisions": []}
    decisions_doc["decisions"] = [
        decision
        for decision in (decisions_doc.get("decisions") or [])
        if decision.get("source_proposal_id") not in proposal_ids
    ]
    decisions_path.write_text(_json(decisions_doc) + "\n", encoding="utf-8")
    print(json.dumps(removed, sort_keys=True), flush=True)
    return removed


def main() -> None:
    data_dir = _validate_environment()
    if sys.argv[1:] == ["--cleanup"]:
        _cleanup(data_dir)
        return
    if sys.argv[1:]:
        raise RuntimeError(f"unknown surface-seed arguments: {sys.argv[1:]!r}")
    result = _seed(data_dir)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
