"""LearnedRule — durable lesson extracted from a user accept/dismiss action.

When the user Accepts a reviewer finding, the system extracts a one-line
"writer-avoidance rule" — a generalized version of the pattern the writer
should not repeat. When the user Dismisses a finding (with a reason), the
system extracts a "reviewer-calibration rule" — a generalized version of
the pattern the reviewer should NOT flag.

Rules start as `draft` and require user approval before they get injected
into future agent system prompts. This prevents one bad accept/dismiss
from corrupting all future drafts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    pass


class LearnedRule(Base, TimestampMixin):
    """A persistent guidance rule extracted from a user action on a finding."""

    __tablename__ = "learned_rules"

    # Composite index over (kind, status) so the rule-injection lookup
    # ("get every approved writer_avoid rule") is a single index scan
    # rather than a full table sweep. Created in migration 0009.
    __table_args__ = (Index("ix_learned_rules_kind_status", "kind", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    # 'writer_avoid' — rule for the Writer Team's prompt, telling it not to
    #   repeat a pattern the user accepted as a real problem.
    # 'reviewer_calibrate' — rule for Reviewer A or B's prompt, telling it
    #   NOT to flag a pattern the user dismissed as a false positive.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # The rule text itself — one or two sentences. Injected verbatim into
    # the agent's system prompt under a "Learned Guidance" header.
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Provenance — where did this rule come from? `source_finding_id` is
    # nullable so user-authored rules (added directly from the UI without
    # a source finding) are supported in the future.
    source_finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("reviewer_findings.id", ondelete="SET NULL"), nullable=True
    )
    # 'accept' or 'dismiss' — what the user did to trigger this extraction.
    source_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Snapshotted from the finding at extraction time so the rule survives
    # if the finding is later deleted. Helps the UI scope rules to "this
    # rule was about uncited claims" without re-joining.
    source_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 'A' or 'B' — only set for reviewer_calibrate rules so we can scope
    # the injection to the right reviewer's prompt. Writer-avoid rules
    # always go to the Writer regardless of which reviewer surfaced them.
    source_reviewer: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # 'draft' — extraction completed but the user hasn't approved.
    # 'approved' — actively injected into agent prompts on next run.
    # 'archived' — superseded or no longer relevant; not injected.
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)

    # Counter for "how many agent calls have used this rule" — surfaces in
    # the UI so the user can see which rules are pulling weight vs. dead.
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
