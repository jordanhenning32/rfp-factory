from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProposalRole, ProposalStatus, RfpDocumentType
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.compliance import ComplianceMatrixItem, GapAnalysis
    from app.models.cost_matrix import CostMatrixArtifact
    from app.models.pricing import PricingPackage
    from app.models.proposal_outcome import ProposalOutcome
    from app.models.section import ProposalSection
    from app.models.team import ProposalTeamMember


class RfpPackage(Base, TimestampMixin):
    __tablename__ = "rfp_packages"

    id: Mapped[int] = mapped_column(primary_key=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    storage_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    proposal: Mapped[Proposal] = relationship(back_populates="rfp_package", uselist=False)
    documents: Mapped[list[RfpPackageDocument]] = relationship(
        back_populates="rfp_package",
        cascade="all, delete-orphan",
    )


class RfpPackageDocument(Base, TimestampMixin):
    __tablename__ = "rfp_package_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    rfp_package_id: Mapped[int] = mapped_column(
        ForeignKey("rfp_packages.id", ondelete="CASCADE"),
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    document_type: Mapped[RfpDocumentType] = mapped_column(
        String(64),
        default=RfpDocumentType.UNKNOWN,
        nullable=False,
    )
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    extracted_text_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    structure_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Role of this document within the RFP package — distinct axis from
    # `document_type` (which is solicitation/SOW/Q&A by content). 'original'
    # = first-batch RFP files, 'amendment' = a buyer amendment (carries
    # sequence_number), 'qa_response' = a Q&A response (no sequence number).
    # NULL = pre-existing rows from before migration 0033; treated as
    # "original" by absence. Plain string column rather than an enum so
    # adding new roles later is a column-update, not a migration.
    document_role: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Buyer-assigned amendment sequence number (e.g. "0001" for Amendment
    # 0001). Always NULL for original + qa_response rows. Set by the
    # Amendments & Q&A tab upload handler.
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    rfp_package: Mapped[RfpPackage] = relationship(back_populates="documents")
    cost_matrix_artifact: Mapped[CostMatrixArtifact | None] = relationship(
        back_populates="source_document",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        single_parent=True,
    )


class Proposal(Base, TimestampMixin):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    rfp_package_id: Mapped[int] = mapped_column(ForeignKey("rfp_packages.id", ondelete="RESTRICT"))

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    agency: Mapped[str | None] = mapped_column(String(300), nullable=True)
    naics: Mapped[str | None] = mapped_column(String(50), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    role: Mapped[ProposalRole] = mapped_column(String(16), default=ProposalRole.PRIME, nullable=False)

    status: Mapped[ProposalStatus] = mapped_column(
        String(40),
        default=ProposalStatus.INTAKING,
        nullable=False,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set by intake's COTS-orientation detector. Toggles a writer
    # system-prompt branch that activates the cots_positioning rule
    # from company_profile._usage_notes_for_agents.
    cots_orientation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    # Cache of the last "Generate Strategy" Sonnet output so the user
    # can re-open it without re-paying. Cleared/overwritten on each
    # regenerate. findings_count is the active-findings snapshot at
    # gen time — UI compares against current count to flag staleness.
    cost_review_strategy_markdown: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    cost_review_strategy_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cost_review_strategy_findings_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # When the user clicks "Approve Team" on the Team tab. Null
    # until they do. Drives UI gates ("Approve team before drafting")
    # and is referenced in agent_runs banners.
    team_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # User-chosen "framing" — the two strategic posture answers that
    # shape every gap response and section draft. NULL on both = "decide
    # per gap" (writer behaves as today; no framing block injected).
    # Set via the Framing panel at the top of the Gaps "Per gap"
    # sub-tab; surfaced to the writer through
    # format_framing_block_for_writer.
    #   teaming_framing: NULL | "open" | "self_perform_only"
    #   build_framing:   NULL | "custom_build_first" | "self_perform_first"
    teaming_framing: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    build_framing: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    # User-chosen pricing scenario (LOW / MEDIUM / HIGH). NULL = no
    # explicit pick; read sites treat NULL as "MEDIUM". Set via the
    # Cost tab's scenario-card click; consumed by Cost Writer, Cost
    # Reviewer, and format_cost_build_block_for_writer. CUSTOM is
    # intentionally not persisted — it's an in-memory what-if mode.
    proposed_scenario: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    # Service-line tag — selects which cost flow + data files apply
    # to this proposal. NULL = legacy default (read as "it_services"
    # so prior proposals see no behavior change). Known values are
    # registered in app/services/service_line.py SERVICE_LINES — the
    # column is a free-form string (not an enum) so adding new
    # service lines later is a registry update, not a migration.
    # Current registry: "it_services" (default IT/labor flow),
    # "payment_systems" (card processing / ACH / recurring billing /
    # donation processing / hospital financing — fee-schedule flow).
    service_line: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    # Payment-systems Market Researcher output (JSON blob). Set by
    # jobs/payment_market_researcher.py when service_line=payment_
    # systems. Includes: recommended pricing structure, comparable
    # processor rate disclosures, volume estimate, profit math.
    # Read by _format_payment_systems_cost_block to inject the data
    # into the Cost Writer's cached prefix. NULL means the scan has
    # not yet been run (button on Cost tab triggers it).
    payment_market_scan_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # User override of the agent-recommended pricing model (payment_
    # systems only). NULL = no override; use the agent's recommended
    # model from payment_market_scan_json. Set values: interchange_
    # plus / flat_rate / tiered / percentage_of_collected. The Cost
    # Volume Writer reads this column to know which model to
    # narrate. See app/services/service_line.py PAYMENT_PRICING_
    # MODELS for the canonical id list.
    selected_pricing_model: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    # Payment-systems Cost Reviewer output (JSON blob). Set by
    # jobs/payment_cost_reviewer.py — adversarial fact-check of the
    # drafted fee narrative against the persisted Payment Market Scan
    # and the brand / risk framing rules. The labor-flow Cost
    # Reviewer persists to cost_review_findings (FK PricingPackage);
    # payment_systems has no PricingPackage rows, so findings live on
    # the proposal row directly. NULL = reviewer hasn't run yet. The
    # Cost Review tab branches on service_line and renders findings
    # from this column for payment_systems proposals.
    payment_cost_review_findings_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # User-curated implementation timeline (Gantt-style phases) shown
    # on the Timeline tab. One JSON document per proposal:
    #   {"anchor_date": "YYYY-MM-DD" | null,
    #    "phases": [{id, phase_name, start_offset, duration,
    #                deliverable, owner, color, order}, ...]}
    # Offsets are days from project start (NTP). anchor_date, when set,
    # lets the UI render absolute dates alongside offsets. Migration
    # 0031 added the column. NULL = no timeline yet.
    timeline_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Section M evaluation-criteria output (JSON blob). Persisted by
    # app/agents/section_m_extractor.py at intake time (after compliance
    # matrix, before shortfall). Structure mirrors the agent's tool schema:
    #   {
    #     "evaluation_method": "best_value" | "lpta" | "trade_off" | "unknown",
    #     "factors": [
    #       {"factor_id": "F1", "factor_name": str, "weight_pct": num|null,
    #        "weight_descriptive": str|null, "scoring_scale": str|null,
    #        "evidence_required": str|null,
    #        "subfactors": [{"name": str, "weight_pct": num|null, "notes": str|null}]},
    #       ...
    #     ],
    #     "section_l_to_m_map": {"REQ-001": ["F1", "F1.1"], ...},
    #     "trade_off_language": str|null,
    #     "lowest_price_clause": str|null,
    #     "extraction_notes": str|null
    #   }
    # NULL = Section M extraction has not yet run for this proposal
    # (older proposals predating migration 0032, or extraction failed).
    # The Evaluation Criteria tab shows an empty state with a re-extract
    # button when this column is NULL. Migration 0032 added the column.
    evaluation_criteria_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Deterministic win-strategy workspace artifacts (migration 0035).
    evaluator_scorecard_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    win_themes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    past_performance_matches_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_to_win_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    red_team_findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    graphics_tables_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    rfp_package: Mapped[RfpPackage] = relationship(back_populates="proposal")
    compliance_items: Mapped[list[ComplianceMatrixItem]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
    )
    gap_analyses: Mapped[list[GapAnalysis]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
    )
    sections: Mapped[list[ProposalSection]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
    )
    pricing_packages: Mapped[list[PricingPackage]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
    )
    cost_matrices: Mapped[list[CostMatrixArtifact]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
    )
    team_members: Mapped[list[ProposalTeamMember]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
    )
    outcome: Mapped[ProposalOutcome | None] = relationship(
        back_populates="proposal",
        uselist=False,
        cascade="all, delete-orphan",
    )
