"""Enumerations referenced across models and agents.

Sourced from RFP_System_Design_v2.md §6, §7.1, §10.
"""

from __future__ import annotations

from enum import StrEnum


class ProposalStatus(StrEnum):
    INTAKING = "intaking"
    AWAITING_SCOPE_SIGNOFF = "awaiting_scope_signoff"
    DRAFTING = "drafting"
    AWAITING_OUTLINE_APPROVAL = "awaiting_outline_approval"
    # Phase 2B reorder — three new gates between outline approval
    # and the Writer Team. Pre-draft sequence: outline → team
    # roster → cost analyst → writer. Each gate is set by the
    # corresponding action's success path; the writer no longer
    # kicks off automatically when the outline is approved.
    AWAITING_TEAM_APPROVAL = "awaiting_team_approval"
    AWAITING_COST_BUILD = "awaiting_cost_build"
    AWAITING_DRAFT = "awaiting_draft"
    DRAFT_IN_PROGRESS = "draft_in_progress"
    DRAFT_READY = "draft_ready"
    REVIEWING = "reviewing"
    PRICING = "pricing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    ARCHIVED = "archived"


class ProposalRole(StrEnum):
    PRIME = "prime"
    SUB = "sub"


class RequirementType(StrEnum):
    SHALL = "shall"
    MUST = "must"
    SHOULD = "should"
    SUBMISSION_FORMAT = "submission_format"
    EVALUATION_CRITERION = "evaluation_criterion"
    MANDATORY_FORM = "mandatory_form"


class RequirementCategory(StrEnum):
    TECHNICAL = "technical"
    MANAGEMENT = "management"
    PAST_PERFORMANCE = "past_performance"
    PERSONNEL = "personnel"
    PRICING = "pricing"
    ADMINISTRATIVE = "administrative"
    CERTIFICATION = "certification"


class ComplianceStatus(StrEnum):
    TO_BE_DRAFTED = "to_be_drafted"
    GAP_FLAGGED = "gap_flagged"
    NOT_APPLICABLE = "not_applicable"
    DRAFTED = "drafted"
    REVIEWED_PASS = "reviewed_pass"


class GapSeverity(StrEnum):
    """Gap severity AND category, flattened into one bucket per design conversation.

    DEAL_BREAKER = no honest mitigation; recommend no-bid.
    MAJOR / MINOR = firm-capability gaps (certifications, geography, staffing,
        contract vehicles, business size, etc.) — things about the FIRM itself.
    TECHNICAL = technical-capability gaps (tech stack, methodology, integration,
        platform support) — things about the WORK the proposal would deliver.
    """

    DEAL_BREAKER = "deal_breaker"
    MAJOR = "major"
    MINOR = "minor"
    TECHNICAL = "technical"


class KbDocumentClass(StrEnum):
    """KB document classes — drives retrieval rules and citation legitimacy.

    See design doc §7.1. Strict separation between past_performance_* and
    prior_proposal_* is critical: pending proposals can ground voice but
    cannot be cited as completed work.
    """

    CORPORATE = "corporate"
    PERSONNEL = "personnel"
    PAST_PERFORMANCE_WON = "past_performance_won"
    PAST_PERFORMANCE_SUBBED = "past_performance_subbed"
    REFERENCES_PROJECT = "references_project"
    REFERENCES_PERSONNEL = "references_personnel"
    PRIOR_PROPOSAL_WON = "prior_proposal_won"
    PRIOR_PROPOSAL_PENDING = "prior_proposal_pending"
    PRIOR_PROPOSAL_LOST = "prior_proposal_lost"
    COMPLIANCE_EVIDENCE = "compliance_evidence"
    AGENCY_CONTEXT = "agency_context"
    BOILERPLATE = "boilerplate"
    PROCUREMENT_CRAFT = "procurement_craft"


# Classes that ARE valid past-performance citation sources.
# Reviewer A enforces: a "Quadratic completed X" claim must trace to one of these.
PAST_PERFORMANCE_CITABLE_CLASSES = frozenset(
    {
        KbDocumentClass.PAST_PERFORMANCE_WON,
        KbDocumentClass.PAST_PERFORMANCE_SUBBED,
    }
)


class KbDocumentStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class ReviewerAgent(StrEnum):
    A_COMPLIANCE_RISK = "A"
    B_PERSUASION_PSYCH = "B"
    # Cross-section consistency checker — runs once per auto-loop after
    # all per-section workers finish, flags conflicts that per-section
    # reviewers can't see (e.g., section 3 says "12 staff", section 7
    # says "30 staff"). Findings persist on each affected section.
    C_CONSISTENCY = "C"


class FindingSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


class FindingCategory(StrEnum):
    COMPLIANCE_GAP = "compliance_gap"
    UNCITED_CLAIM = "uncited_claim"
    HALLUCINATION = "hallucination"
    OVERCOMMITMENT = "overcommitment"
    FORMAT_VIOLATION = "format_violation"
    SHORTFALL_OVERREACH = "shortfall_overreach"
    WEAK_PERSUASION = "weak_persuasion"
    VOICE_INCONSISTENCY = "voice_inconsistency"
    EVALUATOR_MISALIGNMENT = "evaluator_misalignment"
    # Cross-section conflict — produced by the consistency checker
    # (Reviewer C) when two sections make incompatible claims about
    # the same fact (staff count, dates, dollar amounts, etc.).
    CROSS_SECTION_INCONSISTENCY = "cross_section_inconsistency"


class CitationConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PricingScenario(StrEnum):
    """H/M/L scenarios produced by the Cost Analyst (per data/internal_pricing_rules.json
    scenario_definitions). Same labor estimate, different burden / margin / contingency
    assumptions per scenario.

    LOW    — competitive bid (low coverage, 18% margin, 0% contingency)
    MEDIUM — target bid (high coverage, 25% margin, 5% contingency)
    HIGH   — protective bid (high coverage, 30% margin, 10% contingency)
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class MarketPosition(StrEnum):
    """Where a proposed price sits relative to the market_scan band."""

    BELOW = "below"
    IN_BAND = "in_band"
    ABOVE = "above"


class BidRecommendation(StrEnum):
    """Cost Analyst's per-scenario recommendation."""

    BID = "bid"
    WALK_AWAY = "walk_away"
    FLAG_FOR_REVIEW = "flag_for_review"


class CompetitorBidLikelihood(StrEnum):
    """Market Researcher's confidence that a named firm will bid this RFP."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RfpDocumentType(StrEnum):
    MAIN_SOLICITATION = "main_solicitation"
    SOW = "sow"
    QA = "qa"
    AMENDMENT = "amendment"
    ATTACHMENT = "attachment"
    FORM_TEMPLATE = "form_template"
    EVALUATION_CRITERIA = "evaluation_criteria"
    AGENCY_POLICY_REFERENCE = "agency_policy_reference"
    UNKNOWN = "unknown"


class ProposalOutcomeStatus(StrEnum):
    """Post-submission outcome buckets for the proposal_outcomes ledger.

    PENDING   = default — submitted, awaiting buyer
    WON / LOST = explicit outcome
    NO_AWARD  = buyer cancelled / no selection
    WITHDRAWN = we pulled out before award
    """

    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    NO_AWARD = "no_award"
    WITHDRAWN = "withdrawn"
