"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- knowledge_base_documents -------------------------------------------------
    op.create_table(
        "knowledge_base_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("storage_path", sa.String(1000), nullable=False),
        sa.Column("document_class", sa.String(40), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("extracted_text_md", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---- knowledge_base_chunks ---------------------------------------------------
    op.create_table(
        "knowledge_base_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("knowledge_base_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("section_label", sa.String(300), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(80), nullable=True),
        sa.Column("embedding_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_base_chunks_document_id", "knowledge_base_chunks", ["document_id"])

    # ---- rfp_packages ------------------------------------------------------------
    op.create_table(
        "rfp_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uploaded_by", sa.String(120), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("storage_dir", sa.String(500), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "rfp_package_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rfp_package_id", sa.Integer(), sa.ForeignKey("rfp_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("storage_path", sa.String(1000), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("extracted_text_md", sa.Text(), nullable=True),
        sa.Column("structure_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_rfp_package_documents_rfp_package_id", "rfp_package_documents", ["rfp_package_id"])

    # ---- proposals ---------------------------------------------------------------
    op.create_table(
        "proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rfp_package_id", sa.Integer(), sa.ForeignKey("rfp_packages.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("agency", sa.String(300), nullable=True),
        sa.Column("naics", sa.String(50), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_proposals_status", "proposals", ["status"])

    # ---- proposal_sections -------------------------------------------------------
    op.create_table(
        "proposal_sections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section_id", sa.String(64), nullable=False),
        sa.Column("section_title", sa.String(500), nullable=False),
        sa.Column("section_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("draft_text_markdown", sa.Text(), nullable=True),
        sa.Column("current_revision_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compliance_items_addressed_json", sa.JSON(), nullable=False),
        sa.Column("citations_json", sa.JSON(), nullable=False),
        sa.Column("needs_human_placeholders_json", sa.JSON(), nullable=False),
        sa.Column("shortfall_mitigations_applied_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_proposal_sections_proposal_id", "proposal_sections", ["proposal_id"])

    # ---- compliance_matrix_items -------------------------------------------------
    op.create_table(
        "compliance_matrix_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requirement_id", sa.String(64), nullable=False),
        sa.Column("requirement_text", sa.Text(), nullable=False),
        sa.Column("source_doc", sa.String(500), nullable=False),
        sa.Column("source_section", sa.String(200), nullable=True),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("requirement_type", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("compliance_status", sa.String(32), nullable=False),
        sa.Column("linked_response_section_id", sa.Integer(), sa.ForeignKey("proposal_sections.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_compliance_matrix_items_proposal_id", "compliance_matrix_items", ["proposal_id"])

    # ---- gap_analyses ------------------------------------------------------------
    op.create_table(
        "gap_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requirement_id_fk", sa.Integer(), sa.ForeignKey("compliance_matrix_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gap_id", sa.String(64), nullable=False),
        sa.Column("gap_severity", sa.String(32), nullable=False),
        sa.Column("gap_description", sa.Text(), nullable=False),
        sa.Column("current_state", sa.Text(), nullable=True),
        sa.Column("mitigation_options_json", sa.JSON(), nullable=False),
        sa.Column("recommended_mitigation_index", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---- reviewer_findings -------------------------------------------------------
    op.create_table(
        "reviewer_findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_section_id", sa.Integer(), sa.ForeignKey("proposal_sections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_agent", sa.String(8), nullable=False),
        sa.Column("pass_number", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("finding_text", sa.Text(), nullable=False),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column("resolved_in_pass_number", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---- pricing_packages --------------------------------------------------------
    op.create_table(
        "pricing_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fte_breakdown_json", sa.JSON(), nullable=False),
        sa.Column("loaded_labor_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("odcs_json", sa.JSON(), nullable=False),
        sa.Column("subcontractor_costs", sa.Numeric(14, 2), nullable=True),
        sa.Column("indirect_costs_json", sa.JSON(), nullable=False),
        sa.Column("total_proposed_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("pnl_projection_json", sa.JSON(), nullable=False),
        sa.Column("market_comps_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "cost_review_findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pricing_package_id", sa.Integer(), sa.ForeignKey("pricing_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("finding_text", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("category", sa.String(80), nullable=True),
        sa.Column("alternative_scenarios_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---- agent_runs --------------------------------------------------------------
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_name", sa.String(80), nullable=False),
        sa.Column("model_used", sa.String(80), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_runs_proposal_id", "agent_runs", ["proposal_id"])

    # ---- company_profile_versions ------------------------------------------------
    op.create_table(
        "company_profile_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("effective_from", sa.String(40), nullable=True),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "internal_pricing_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("effective_from", sa.String(40), nullable=True),
        sa.Column("rules_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("internal_pricing_rules")
    op.drop_table("company_profile_versions")
    op.drop_index("ix_agent_runs_proposal_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_table("cost_review_findings")
    op.drop_table("pricing_packages")
    op.drop_table("reviewer_findings")
    op.drop_table("gap_analyses")
    op.drop_index("ix_compliance_matrix_items_proposal_id", table_name="compliance_matrix_items")
    op.drop_table("compliance_matrix_items")
    op.drop_index("ix_proposal_sections_proposal_id", table_name="proposal_sections")
    op.drop_table("proposal_sections")
    op.drop_index("ix_proposals_status", table_name="proposals")
    op.drop_table("proposals")
    op.drop_index("ix_rfp_package_documents_rfp_package_id", table_name="rfp_package_documents")
    op.drop_table("rfp_package_documents")
    op.drop_table("rfp_packages")
    op.drop_index("ix_knowledge_base_chunks_document_id", table_name="knowledge_base_chunks")
    op.drop_table("knowledge_base_chunks")
    op.drop_table("knowledge_base_documents")
