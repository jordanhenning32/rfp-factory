"""Buyer-authored cost-matrix artifacts attached to a proposal.

The source workbook is immutable.  This row stores the template-specific
analysis/mapping manifest and, once pricing is approved, points at a generated
derivative workbook in the proposal's managed package directory.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.proposal import Proposal, RfpPackageDocument


class CostMatrixArtifact(Base, TimestampMixin):
    __tablename__ = "cost_matrix_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "source_document_id",
            name="uq_cost_matrix_artifacts_source_document_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("rfp_package_documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Free-form string by design so the lifecycle can evolve without an enum
    # migration. Current values are maintained by app.services.cost_matrix.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="detected", server_default="detected"
    )
    template_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_version: Mapped[str] = mapped_column(String(24), nullable=False)
    analysis_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    mapping_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    human_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    proposal: Mapped[Proposal] = relationship(back_populates="cost_matrices")
    source_document: Mapped[RfpPackageDocument] = relationship(
        back_populates="cost_matrix_artifact"
    )
    outputs: Mapped[list[CostMatrixOutput]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        order_by="CostMatrixOutput.version",
    )


class CostMatrixOutput(Base, TimestampMixin):
    """Immutable generated revision of a cost-matrix template."""

    __tablename__ = "cost_matrix_outputs"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "version",
            name="uq_cost_matrix_outputs_artifact_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("cost_matrix_artifacts.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    pricing_scenario: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pricing_basis_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_provenance_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    mapping_snapshot_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    output_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    output_storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    artifact: Mapped[CostMatrixArtifact] = relationship(back_populates="outputs")
