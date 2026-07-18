"""Amendment ingestion audit row.

One AmendmentRun per Amendment / Q&A document processed against a proposal.
Tracks the daemon-thread's lifecycle (running -> completed | failed) and
persists the AmendmentApplyReport JSON on success / the exception text on
failure. Mirrors the shape of `AgentRun` but is dedicated to the amendment
pipeline so the cost-tracking + audit semantics on AgentRun stay clean.

`status` is a free-form string (not an enum) — the value set is tiny and
stable ('running' | 'completed' | 'failed') and adding an enum would ripple
into every existing AgentRun-style query path.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AmendmentRun(Base, TimestampMixin):
    """Audit row for one amendment / Q&A ingestion attempt.

    Lifecycle:
      - Insert with status='running', started_at=now when the daemon thread
        begins processing.
      - On success: status='completed', completed_at=now,
        report_json=json.dumps(AmendmentApplyReport.as_dict()).
      - On failure: status='failed', error_text=str(exc)[:2000].
    """

    __tablename__ = "amendment_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("rfp_package_documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="running",
        nullable=False,
    )
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
