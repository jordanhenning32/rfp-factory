from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AgentRunStatus
from app.db.base import Base, TimestampMixin


class AgentRun(Base, TimestampMixin):
    """One execution of one agent against one proposal. Drives the cost dashboard
    (design doc §14) and the audit trail in the proposal review UI (§9.1).
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )

    agent_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[AgentRunStatus] = mapped_column(
        String(16),
        default=AgentRunStatus.QUEUED,
        nullable=False,
    )

    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
