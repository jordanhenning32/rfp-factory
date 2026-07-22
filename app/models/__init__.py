"""SQLAlchemy ORM models. Schema sourced from RFP_System_Design_v2.md §10.

Importing this module registers all models on the Base.metadata so that
Alembic autogenerate sees them.
"""

from app.models.agent_run import AgentRun
from app.models.amendment import AmendmentRun
from app.models.company_profile import CompanyProfileVersion, InternalPricingRules
from app.models.compliance import ComplianceMatrixItem, GapAnalysis
from app.models.cost_matrix import CostMatrixArtifact, CostMatrixOutput
from app.models.kb import KnowledgeBaseChunk, KnowledgeBaseDocument
from app.models.learned_rule import LearnedRule
from app.models.market_scan import (
    MarketScan,
    MarketScanComparableAward,
    MarketScanCompetitor,
)
from app.models.polish_edit import PolishEdit
from app.models.pricing import (
    CostReviewFinding,
    PricingPackage,
    PricingPackageLine,
)
from app.models.profile_suggestion import ProfileSuggestion
from app.models.proposal import Proposal, RfpPackage, RfpPackageDocument
from app.models.proposal_outcome import ProposalOutcome
from app.models.reviewer import ReviewerFinding
from app.models.section import ProposalSection
from app.models.submission_commitment import SubmissionCommitment
from app.models.team import ProposalTeamMember

__all__ = [
    "AgentRun",
    "AmendmentRun",
    "ComplianceMatrixItem",
    "CompanyProfileVersion",
    "CostReviewFinding",
    "CostMatrixArtifact",
    "CostMatrixOutput",
    "GapAnalysis",
    "InternalPricingRules",
    "KnowledgeBaseChunk",
    "KnowledgeBaseDocument",
    "LearnedRule",
    "MarketScan",
    "MarketScanComparableAward",
    "MarketScanCompetitor",
    "PolishEdit",
    "PricingPackage",
    "PricingPackageLine",
    "ProfileSuggestion",
    "Proposal",
    "ProposalOutcome",
    "ProposalSection",
    "ProposalTeamMember",
    "ReviewerFinding",
    "RfpPackage",
    "RfpPackageDocument",
    "SubmissionCommitment",
]
