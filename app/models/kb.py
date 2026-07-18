from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import KbDocumentClass, KbDocumentStatus
from app.db.base import Base, TimestampMixin


class KnowledgeBaseDocument(Base, TimestampMixin):
    """A single KB document with class metadata that determines citation legitimacy.

    Citation rule (design doc §7.1, enforced by Reviewer A): past performance
    citations must trace to past_performance_won or past_performance_subbed.
    Pending/lost prior proposals can ground voice but cannot be cited as
    completed work.
    """

    __tablename__ = "knowledge_base_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    document_class: Mapped[KbDocumentClass] = mapped_column(String(40), nullable=False)
    # Free-form tags: agency names, NAICS, scope domains, etc.
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[KbDocumentStatus] = mapped_column(
        String(16),
        default=KbDocumentStatus.PENDING,
        nullable=False,
    )

    extracted_text_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Open-ended metadata: contract value, period, role, status (won/pending/lost), etc.
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    chunks: Mapped[list[KnowledgeBaseChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class KnowledgeBaseChunk(Base, TimestampMixin):
    """A retrievable chunk of a KB document with its embedding.

    Embeddings stored as bytes (numpy bytes) for SQLite portability. When the
    KB outgrows SQLite, swap in pgvector or a dedicated vector store and
    keep this schema as the system of record.
    """

    __tablename__ = "knowledge_base_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_base_documents.id", ondelete="CASCADE"),
        index=True,
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    section_label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)

    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    document: Mapped[KnowledgeBaseDocument] = relationship(back_populates="chunks")
