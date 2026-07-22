"""Deterministic win-strategy helpers.

The strategy workspace turns existing proposal data into evaluator-facing
artifacts: a Section M scorecard, win themes, relevant past performance,
price posture, red-team risks, and recommended tables. These helpers avoid
LLM calls by default so the user can refresh them freely while iterating.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

import app.db.session as db_session
from app.core.company_profile import get_company_profile
from app.models import (
    ComplianceMatrixItem,
    GapAnalysis,
    PricingPackage,
    Proposal,
    ProposalSection,
    ProposalTeamMember,
    ReviewerFinding,
)
from app.services.proposal_access import ensure_proposal_mutable

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{2,}")
_STOPWORDS = frozenset(
    {
        "and", "are", "but", "can", "for", "from", "has", "have", "into",
        "its", "not", "our", "shall", "should", "the", "their", "this",
        "that", "with", "will", "within", "without", "you", "your", "must",
        "may", "all", "any", "each", "using", "use", "provide", "services",
        "service", "contract", "proposal", "offeror", "government",
    }
)

_ARTIFACT_FIELDS = {
    "evaluator_scorecard": "evaluator_scorecard_json",
    "win_themes": "win_themes_json",
    "past_performance_matches": "past_performance_matches_json",
    "price_to_win": "price_to_win_json",
    "red_team_findings": "red_team_findings_json",
    "graphics_tables": "graphics_tables_json",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if t.lower() not in _STOPWORDS and len(t) >= 3
    }


def _token_counter(parts: list[str]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for part in parts:
        counter.update(_tokens(part))
    return counter


def _status_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _load_strategy_from_proposal(p: Proposal | None) -> dict[str, Any]:
    if p is None:
        return {k: None for k in _ARTIFACT_FIELDS}
    return {
        key: _loads(getattr(p, attr), None)
        for key, attr in _ARTIFACT_FIELDS.items()
    }


def load_win_strategy(proposal_id: int) -> dict[str, Any]:
    """Return every persisted strategy artifact for a proposal."""
    with db_session.session_scope() as db:
        return _load_strategy_from_proposal(db.get(Proposal, proposal_id))


def _persist(proposal_id: int, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    attr = _ARTIFACT_FIELDS[key]
    with db_session.session_scope() as db:
        proposal = ensure_proposal_mutable(
            db, proposal_id, operation="regenerate win strategy",
        )
        if proposal is None:
            raise ValueError(f"proposal {proposal_id} not found")
        setattr(proposal, attr, _dumps(payload))
    return payload


def _snapshot(proposal_id: int) -> dict[str, Any]:
    """Snapshot proposal data as primitives so generators do no ORM work."""
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            raise ValueError(f"proposal {proposal_id} not found")

        compliance = (
            db.execute(
                select(ComplianceMatrixItem)
                .where(
                    ComplianceMatrixItem.proposal_id == proposal_id,
                    ComplianceMatrixItem.status == "active",
                )
                .order_by(ComplianceMatrixItem.id)
            )
            .scalars()
            .all()
        )
        comp_rows = [
            {
                "pk": i.id,
                "requirement_id": i.requirement_id,
                "requirement_text": i.requirement_text or "",
                "requirement_type": _status_value(i.requirement_type),
                "category": _status_value(i.category),
                "weight": float(i.weight) if i.weight is not None else None,
                "source_doc": i.source_doc,
                "source_page": i.source_page,
            }
            for i in compliance
        ]
        req_pk_to_id = {r["pk"]: r["requirement_id"] for r in comp_rows}

        gap_rows = (
            db.execute(
                select(GapAnalysis)
                .where(GapAnalysis.proposal_id == proposal_id)
                .order_by(GapAnalysis.id)
            )
            .scalars()
            .all()
        )
        gaps = [
            {
                "id": g.id,
                "gap_id": g.gap_id,
                "req_id": req_pk_to_id.get(g.requirement_id_fk),
                "severity": _status_value(g.gap_severity),
                "current_state": g.current_state or "",
                "resolved": bool(g.resolved),
                "selected_mitigation_index": g.selected_mitigation_index,
                "recommended_mitigation_index": g.recommended_mitigation_index,
                "selected_partner_name": g.selected_partner_name,
                "mitigation_options": list(g.mitigation_options_json or []),
            }
            for g in gap_rows
            if req_pk_to_id.get(g.requirement_id_fk)
        ]

        section_rows = (
            db.execute(
                select(ProposalSection)
                .where(ProposalSection.proposal_id == proposal_id)
                .order_by(ProposalSection.section_order, ProposalSection.id)
            )
            .scalars()
            .all()
        )
        sections = [
            {
                "pk": s.id,
                "section_id": s.section_id,
                "section_title": s.section_title,
                "section_order": s.section_order,
                "section_brief": s.section_brief or "",
                "draft_text": s.draft_text_markdown or "",
                "citations": list(s.citations_json or []),
                "needs_human": list(s.needs_human_placeholders_json or []),
                "compliance_items": list(s.compliance_items_addressed_json or []),
                "requires_cost_analysis": bool(s.requires_cost_analysis),
                "excluded_from_draft": bool(s.excluded_from_draft),
            }
            for s in section_rows
        ]

        open_finding_rows = (
            db.execute(
                select(ReviewerFinding, ProposalSection)
                .join(ProposalSection, ProposalSection.id == ReviewerFinding.proposal_section_id)
                .where(
                    ProposalSection.proposal_id == proposal_id,
                    ReviewerFinding.resolved_in_pass_number.is_(None),
                    ReviewerFinding.dismissed_at.is_(None),
                )
            )
            .all()
        )
        open_findings = [
            {
                "section_pk": section.id,
                "section_id": section.section_id,
                "severity": _status_value(finding.severity),
                "category": _status_value(finding.category),
                "finding_text": finding.finding_text or "",
            }
            for finding, section in open_finding_rows
        ]

        pricing_rows = (
            db.execute(
                select(PricingPackage)
                .where(PricingPackage.proposal_id == proposal_id)
                .order_by(PricingPackage.scenario)
            )
            .scalars()
            .all()
        )
        pricing = [
            {
                "scenario": p.scenario,
                "total_proposed_price": (
                    float(p.total_proposed_price)
                    if p.total_proposed_price is not None else None
                ),
                "loaded_labor_cost": (
                    float(p.loaded_labor_cost)
                    if p.loaded_labor_cost is not None else None
                ),
                "vs_market_position": p.vs_market_position,
                "bid_recommendation": p.bid_recommendation,
                "recommendation_rationale": p.recommendation_rationale,
                "pnl": dict(p.pnl_projection_json or {}),
                "indirect": dict(p.indirect_costs_json or {}),
            }
            for p in pricing_rows
        ]

        team_rows = (
            db.execute(
                select(ProposalTeamMember)
                .where(ProposalTeamMember.proposal_id == proposal_id)
                .order_by(ProposalTeamMember.id)
            )
            .scalars()
            .all()
        )
        team = [
            {
                "role_name": t.role_name,
                "person_kind": t.person_kind,
                "assigned_person": t.assigned_person,
                "labor_category": t.labor_category,
                "time_allocation_pct": t.time_allocation_pct,
                "bio_summary": t.bio_summary,
            }
            for t in team_rows
        ]

        persisted = _load_strategy_from_proposal(proposal)
        criteria = _loads(proposal.evaluation_criteria_json, {})

        return {
            "proposal": {
                "id": proposal.id,
                "title": proposal.title,
                "agency": proposal.agency,
                "naics": proposal.naics,
                "status": _status_value(proposal.status),
                "notes": proposal.notes or "",
                "cots_orientation": bool(proposal.cots_orientation),
                "service_line": proposal.service_line or "it_services",
                "proposed_scenario": proposal.proposed_scenario or "MEDIUM",
            },
            "criteria": criteria if isinstance(criteria, dict) else {},
            "compliance": comp_rows,
            "gaps": gaps,
            "sections": sections,
            "open_findings": open_findings,
            "pricing": pricing,
            "team": team,
            "persisted": persisted,
        }


def _criteria_factors(snap: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    criteria = snap["criteria"] or {}
    factors = list(criteria.get("factors") or [])
    mapping = {
        str(req): [str(fid) for fid in (fids or [])]
        for req, fids in (criteria.get("section_l_to_m_map") or {}).items()
    }
    if factors:
        return factors, mapping

    by_category: dict[str, list[str]] = {}
    for item in snap["compliance"]:
        by_category.setdefault(item["category"], []).append(item["requirement_id"])
    derived = []
    for idx, (category, req_ids) in enumerate(sorted(by_category.items()), start=1):
        fid = f"D{idx}"
        derived.append(
            {
                "factor_id": fid,
                "factor_name": category.replace("_", " ").title(),
                "weight_pct": None,
                "weight_descriptive": "Derived from compliance categories",
                "evidence_required": "Narrative evidence mapped from active compliance items.",
                "subfactors": [],
            }
        )
        for req_id in req_ids:
            mapping.setdefault(req_id, []).append(fid)
    return derived, mapping


def generate_evaluator_scorecard(proposal_id: int) -> dict[str, Any]:
    """Build and persist a factor-by-factor source-selection scorecard."""
    snap = _snapshot(proposal_id)
    factors, req_to_factors = _criteria_factors(snap)
    req_by_id = {item["requirement_id"]: item for item in snap["compliance"]}
    sections = snap["sections"]
    gaps = snap["gaps"]
    open_findings = snap["open_findings"]

    factor_cards: list[dict[str, Any]] = []
    weighted_scores: list[tuple[float, float]] = []
    total_default_weight = 1.0 / max(len(factors), 1)

    for factor in factors:
        fid = str(factor.get("factor_id") or "?")
        fname = str(factor.get("factor_name") or fid)
        req_ids = [
            req_id
            for req_id, fids in req_to_factors.items()
            if fid in fids and req_id in req_by_id
        ]
        mapped_sections = [
            s for s in sections
            if set(s["compliance_items"]).intersection(req_ids)
            and not s["excluded_from_draft"]
        ]
        drafted_sections = [s for s in mapped_sections if s["draft_text"].strip()]
        unresolved_gaps = [
            g for g in gaps
            if g["req_id"] in req_ids
            and not g["resolved"]
            and g["selected_mitigation_index"] is None
        ]
        managed_gaps = [
            g for g in gaps
            if g["req_id"] in req_ids
            and (g["resolved"] or g["selected_mitigation_index"] is not None)
        ]
        section_pks = {s["pk"] for s in mapped_sections}
        factor_findings = [
            f for f in open_findings if f["section_pk"] in section_pks
        ]

        score = 100.0
        if req_ids and not mapped_sections:
            score -= 30
        if mapped_sections and not drafted_sections:
            score -= 25
        score -= min(35, 12 * len(unresolved_gaps))
        score -= min(24, 8 * len([f for f in factor_findings if f["severity"] in ("CRITICAL", "MAJOR")]))
        if managed_gaps:
            score -= min(8, 2 * len(managed_gaps))
        score = max(0, min(100, score))

        if score >= 85:
            band = "Strong"
        elif score >= 70:
            band = "Competitive"
        elif score >= 50:
            band = "At Risk"
        else:
            band = "Not Ready"

        strengths: list[str] = []
        weaknesses: list[str] = []
        risks: list[str] = []
        if drafted_sections:
            strengths.append(
                f"{len(drafted_sections)} drafted section(s) address this factor."
            )
        if managed_gaps:
            strengths.append(
                f"{len(managed_gaps)} gap(s) already have selected or resolved mitigations."
            )
        if not req_ids:
            risks.append("No compliance items are mapped to this factor yet.")
        if req_ids and not mapped_sections:
            weaknesses.append("No outline section is explicitly assigned to the mapped requirements.")
        if mapped_sections and not drafted_sections:
            weaknesses.append("Mapped sections are not drafted yet.")
        if unresolved_gaps:
            risks.append(f"{len(unresolved_gaps)} unresolved gap(s) may become evaluator weaknesses.")
        if factor_findings:
            risks.append(f"{len(factor_findings)} open reviewer finding(s) touch mapped sections.")
        if not strengths:
            strengths.append("No positive evidence identified yet; add proof before final review.")

        weight = factor.get("weight_pct")
        numeric_weight = float(weight) / 100.0 if isinstance(weight, (int, float)) else total_default_weight
        weighted_scores.append((score, numeric_weight))

        factor_cards.append(
            {
                "factor_id": fid,
                "factor_name": fname,
                "weight_pct": weight,
                "weight_descriptive": factor.get("weight_descriptive"),
                "mapped_requirement_ids": req_ids,
                "mapped_section_ids": [s["section_id"] for s in mapped_sections],
                "drafted_section_ids": [s["section_id"] for s in drafted_sections],
                "open_findings_count": len(factor_findings),
                "unresolved_gap_ids": [g["gap_id"] for g in unresolved_gaps],
                "managed_gap_ids": [g["gap_id"] for g in managed_gaps],
                "score": round(score, 1),
                "readiness_band": band,
                "likely_strengths": strengths,
                "likely_weaknesses": weaknesses,
                "evaluation_risks": risks,
                "confidence": "HIGH" if req_ids else "MEDIUM",
            }
        )

    denom = sum(w for _, w in weighted_scores) or 1.0
    overall = sum(score * weight for score, weight in weighted_scores) / denom
    blockers = sum(1 for card in factor_cards if card["readiness_band"] in ("At Risk", "Not Ready"))
    payload = {
        "generated_at": _now_iso(),
        "proposal_id": proposal_id,
        "method": (snap["criteria"] or {}).get("evaluation_method", "unknown"),
        "overall_score": round(overall, 1),
        "overall_readiness": (
            "Strong" if overall >= 85 and blockers == 0
            else "Competitive" if overall >= 70 and blockers <= 1
            else "At Risk" if overall >= 50
            else "Not Ready"
        ),
        "blocker_count": blockers,
        "factors": factor_cards,
        "next_actions": _scorecard_next_actions(factor_cards),
    }
    return _persist(proposal_id, "evaluator_scorecard", payload)


def _scorecard_next_actions(factor_cards: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for card in factor_cards:
        fid = card["factor_id"]
        if card["unresolved_gap_ids"]:
            actions.append(
                f"Resolve or select mitigations for {fid}: "
                + ", ".join(card["unresolved_gap_ids"][:5])
            )
        if card["mapped_requirement_ids"] and not card["mapped_section_ids"]:
            actions.append(f"Assign {fid} requirements to an outline section.")
        if card["mapped_section_ids"] and not card["drafted_section_ids"]:
            actions.append(f"Draft mapped sections for {fid}.")
        if card["open_findings_count"]:
            actions.append(f"Clear reviewer findings touching {fid}.")
    return actions[:8]


def generate_win_themes(proposal_id: int) -> dict[str, Any]:
    snap = _snapshot(proposal_id)
    scorecard = snap["persisted"].get("evaluator_scorecard") or generate_evaluator_scorecard(proposal_id)
    top_factors = sorted(
        scorecard.get("factors") or [],
        key=lambda f: (f.get("weight_pct") is None, -(f.get("weight_pct") or 0), f.get("factor_id", "")),
    )[:3]
    profile = get_company_profile()
    certs = profile.get("certifications") or []
    past_perf = profile.get("past_performance") or []
    team = snap["team"]
    pricing = snap["pricing"]
    proposal = snap["proposal"]

    themes: list[dict[str, Any]] = []
    themes.append(
        {
            "id": "WT-001",
            "title": "Evaluator-mapped compliance discipline",
            "discriminator": "Every major claim is traceable to a scored factor, requirement, section, and evidence source.",
            "buyer_pain": "Evaluators need a low-friction path from Section M criteria to proposal proof.",
            "proof_points": [
                f"{len(snap['compliance'])} active compliance item(s) mapped into the working proposal.",
                f"{len(scorecard.get('factors') or [])} evaluation factor(s) tracked in the scorecard.",
            ],
            "linked_factor_ids": [f["factor_id"] for f in top_factors],
            "section_targets": _sections_for_factors(top_factors),
            "status": "active",
        }
    )
    if team:
        named = [t for t in team if (t.get("assigned_person") or "").strip()]
        themes.append(
            {
                "id": "WT-002",
                "title": "Named small-team accountability",
                "discriminator": "Quadratic proposes accountable named roles instead of an anonymous bench.",
                "buyer_pain": "Small agencies need the people in the proposal to be the people doing the work.",
                "proof_points": [
                    f"{len(named)} named team member(s) in the approved roster.",
                    f"{len(team)} delivery role(s) with labor category and allocation context.",
                ],
                "linked_factor_ids": [f["factor_id"] for f in top_factors[:2]],
                "section_targets": ["Team", "Management", "Staffing"],
                "status": "active",
            }
        )
    if past_perf:
        top_project = past_perf[0]
        themes.append(
            {
                "id": "WT-003",
                "title": "Relevant public-sector modernization proof",
                "discriminator": "Quadratic can point to citable modernization and integration work instead of generic capability language.",
                "buyer_pain": "The buyer needs confidence that the vendor has handled comparable mission and integration risk.",
                "proof_points": [
                    f"{top_project.get('project', 'Past performance')} - {top_project.get('customer', 'customer')}",
                    "Past performance sources remain restricted to citable won/subbed classes.",
                ],
                "linked_factor_ids": [f["factor_id"] for f in top_factors],
                "section_targets": ["Past Performance", "Technical Approach"],
                "status": "active",
            }
        )
    if proposal["cots_orientation"]:
        themes.append(
            {
                "id": "WT-004",
                "title": "COTS-equivalent speed with fit-to-workflow control",
                "discriminator": "Quadratic frames custom delivery around schedule, risk parity, and workflow fit rather than conceding to generic products.",
                "buyer_pain": "The buyer wants low implementation risk without accepting a rigid product mismatch.",
                "proof_points": [
                    "COTS orientation detected during intake.",
                    "Company profile includes the mandatory cots_positioning guidance.",
                ],
                "linked_factor_ids": [f["factor_id"] for f in top_factors[:2]],
                "section_targets": ["Technical Approach", "Risk", "Implementation"],
                "status": "active",
            }
        )
    if pricing:
        selected = next((p for p in pricing if p["scenario"] == proposal["proposed_scenario"]), pricing[0])
        price = selected.get("total_proposed_price")
        themes.append(
            {
                "id": "WT-005",
                "title": "Best-value price discipline",
                "discriminator": "The price narrative can tie scenario selection to evaluator risk, not just cost arithmetic.",
                "buyer_pain": "Source selection needs a defensible reason to prefer value over the lowest apparent price.",
                "proof_points": [
                    f"Selected scenario: {selected['scenario']}.",
                    f"Proposed price: ${price:,.0f}." if price else "Cost build available for scenario comparison.",
                ],
                "linked_factor_ids": [f["factor_id"] for f in top_factors],
                "section_targets": ["Cost", "Executive Summary"],
                "status": "active",
            }
        )
    if certs:
        themes.append(
            {
                "id": f"WT-{len(themes) + 1:03d}",
                "title": "Verified credential boundary",
                "discriminator": "The proposal claims only credentials in the canonical profile, reducing protest and downgrade risk.",
                "buyer_pain": "Evaluators penalize overclaiming and unsupported security/compliance statements.",
                "proof_points": certs[:4],
                "linked_factor_ids": [f["factor_id"] for f in top_factors[:2]],
                "section_targets": ["Security", "Compliance", "Technical Approach"],
                "status": "active",
            }
        )

    payload = {
        "generated_at": _now_iso(),
        "proposal_id": proposal_id,
        "themes": themes[:5],
    }
    return _persist(proposal_id, "win_themes", payload)


def _sections_for_factors(factors: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for factor in factors:
        for sid in factor.get("mapped_section_ids") or []:
            if sid not in seen:
                seen.append(sid)
    return seen[:8]


def generate_past_performance_matches(proposal_id: int) -> dict[str, Any]:
    snap = _snapshot(proposal_id)
    profile = get_company_profile()
    projects = profile.get("past_performance") or []
    corpus = [
        snap["proposal"]["title"] or "",
        snap["proposal"]["agency"] or "",
        snap["proposal"]["notes"] or "",
    ]
    corpus.extend(i["requirement_text"] for i in snap["compliance"])
    corpus.extend(s["section_title"] + " " + s["section_brief"] for s in snap["sections"])
    opportunity_terms = _token_counter(corpus)
    agency_terms = _tokens(snap["proposal"]["agency"] or "")

    matches: list[dict[str, Any]] = []
    for project in projects:
        project_text = " ".join(
            [
                str(project.get("project") or ""),
                str(project.get("customer") or ""),
                str(project.get("role") or ""),
                str(project.get("scope") or ""),
                " ".join(str(t) for t in project.get("tags") or []),
            ]
        )
        project_terms = _tokens(project_text)
        overlap = project_terms.intersection(opportunity_terms)
        weighted_overlap = sum(opportunity_terms[t] for t in overlap)
        agency_overlap = project_terms.intersection(agency_terms)
        citable = project.get("citation_class") in {
            "past_performance_won",
            "past_performance_subbed",
        }
        score = weighted_overlap * 6 + len(agency_overlap) * 15
        if citable:
            score += 12
        if any(t in project_terms for t in {"cms", "medicare", "medicaid", "claims", "payment"}):
            score += 5
        score = min(100, score)
        if score >= 70:
            fit = "HIGH"
        elif score >= 40:
            fit = "MEDIUM"
        else:
            fit = "LOW"
        matches.append(
            {
                "project": project.get("project"),
                "customer": project.get("customer"),
                "role": project.get("role"),
                "scope": project.get("scope"),
                "citation_class": project.get("citation_class"),
                "citable": citable,
                "fit_score": round(score, 1),
                "fit": fit,
                "matched_terms": sorted(overlap)[:14],
                "recommended_use": _past_perf_use(fit, citable),
            }
        )

    matches.sort(key=lambda m: (-m["fit_score"], m["project"] or ""))
    payload = {
        "generated_at": _now_iso(),
        "proposal_id": proposal_id,
        "matches": matches,
        "top_citable_projects": [m for m in matches if m["citable"]][:3],
    }
    return _persist(proposal_id, "past_performance_matches", payload)


def _past_perf_use(fit: str, citable: bool) -> str:
    if not citable:
        return "Use for internal voice only; do not cite as completed work."
    if fit == "HIGH":
        return "Lead citation candidate for past performance and technical proof."
    if fit == "MEDIUM":
        return "Secondary proof point; cite where terms align with the section."
    return "Use sparingly; relevance is limited for this RFP."


def generate_price_to_win(proposal_id: int) -> dict[str, Any]:
    snap = _snapshot(proposal_id)
    pricing = snap["pricing"]
    method = (snap["criteria"] or {}).get("evaluation_method", "unknown")
    selected = snap["proposal"]["proposed_scenario"]
    if not pricing:
        payload = {
            "generated_at": _now_iso(),
            "proposal_id": proposal_id,
            "status": "not_ready",
            "source_selection_method": method,
            "recommended_scenario": None,
            "rationale": "No pricing packages exist yet. Run the Cost Analyst or payment cost flow first.",
            "scenarios": [],
            "guardrails": ["Do not draft a price-to-win claim until pricing exists."],
            "risks": ["Price reasonableness cannot be assessed without a cost build."],
        }
        return _persist(proposal_id, "price_to_win", payload)

    def _scenario_rank(row: dict[str, Any]) -> tuple[int, float]:
        order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CUSTOM": 3}
        price = row.get("total_proposed_price")
        return (order.get(row["scenario"], 9), float(price or math.inf))

    by_scenario = {p["scenario"]: p for p in pricing}
    low_bid_ok = [
        p for p in pricing
        if p.get("bid_recommendation") != "walk_away"
        and p.get("total_proposed_price") is not None
    ]
    if method == "lpta":
        recommended = min(low_bid_ok or pricing, key=lambda p: p.get("total_proposed_price") or math.inf)
        posture = "price-dominant"
        rationale = "LPTA posture: recommend the lowest scenario that does not violate bid guardrails."
    elif method in {"trade_off", "best_value"}:
        recommended = by_scenario.get(selected) or by_scenario.get("MEDIUM") or sorted(pricing, key=_scenario_rank)[0]
        posture = "value-tradeoff"
        rationale = "Best-value posture: defend the selected scenario by tying price to risk reduction and evaluator strengths."
    else:
        recommended = by_scenario.get(selected) or by_scenario.get("MEDIUM") or sorted(pricing, key=_scenario_rank)[0]
        posture = "balanced"
        rationale = "Unknown method: use the selected or medium scenario and avoid premium-price claims unless Section M supports them."

    scenarios = []
    for row in sorted(pricing, key=_scenario_rank):
        price = row.get("total_proposed_price")
        pnl = row.get("pnl") or {}
        gross_margin = pnl.get("gross_margin_pct")
        scenarios.append(
            {
                "scenario": row["scenario"],
                "total_proposed_price": price,
                "gross_margin_pct": gross_margin,
                "vs_market_position": row.get("vs_market_position"),
                "bid_recommendation": row.get("bid_recommendation"),
                "narrative_role": (
                    "recommended" if row["scenario"] == recommended["scenario"]
                    else "fallback" if row["scenario"] == selected
                    else "comparison"
                ),
            }
        )

    risks = []
    if recommended.get("vs_market_position") == "above":
        risks.append("Recommended scenario is above market; proposal must justify premium with scored strengths.")
    if recommended.get("bid_recommendation") == "walk_away":
        risks.append("Recommended scenario is marked walk-away; review margin and scope before using.")
    if method == "lpta" and recommended["scenario"] != "LOW":
        risks.append("LPTA method but LOW scenario is not recommended; price competitiveness may be weak.")

    payload = {
        "generated_at": _now_iso(),
        "proposal_id": proposal_id,
        "status": "ready",
        "source_selection_method": method,
        "posture": posture,
        "recommended_scenario": recommended["scenario"],
        "rationale": rationale,
        "scenarios": scenarios,
        "guardrails": [
            "Do not exceed GSA OLM ceiling rates.",
            "Use value language only when tied to Section M strengths.",
            "If price is above market, explicitly connect premium to reduced performance risk.",
        ],
        "risks": risks,
    }
    return _persist(proposal_id, "price_to_win", payload)


def generate_red_team_findings(proposal_id: int) -> dict[str, Any]:
    snap = _snapshot(proposal_id)
    findings: list[dict[str, Any]] = []
    scorecard = snap["persisted"].get("evaluator_scorecard") or generate_evaluator_scorecard(proposal_id)
    themes = snap["persisted"].get("win_themes")
    pp = snap["persisted"].get("past_performance_matches")
    price = snap["persisted"].get("price_to_win")

    for card in scorecard.get("factors") or []:
        if card["readiness_band"] in {"At Risk", "Not Ready"}:
            findings.append(
                {
                    "severity": "MAJOR" if card["readiness_band"] == "At Risk" else "CRITICAL",
                    "area": "Evaluation factor",
                    "section_id": None,
                    "factor_ids": [card["factor_id"]],
                    "finding": f"{card['factor_id']} is {card['readiness_band']}.",
                    "suggested_fix": "; ".join(card.get("evaluation_risks") or card.get("likely_weaknesses") or [])[:400],
                }
            )

    for section in snap["sections"]:
        if section["excluded_from_draft"]:
            continue
        draft = section["draft_text"]
        if not draft.strip() and not section["requires_cost_analysis"]:
            findings.append(
                {
                    "severity": "MAJOR",
                    "area": "Undrafted section",
                    "section_id": section["section_id"],
                    "factor_ids": [],
                    "finding": "Section has no draft text.",
                    "suggested_fix": "Draft the section before final review.",
                }
            )
            continue
        if section["needs_human"]:
            findings.append(
                {
                    "severity": "MAJOR",
                    "area": "Unresolved placeholders",
                    "section_id": section["section_id"],
                    "factor_ids": [],
                    "finding": f"{len(section['needs_human'])} NEEDS_HUMAN marker(s) remain.",
                    "suggested_fix": "Resolve or rewrite placeholders before submission.",
                }
            )
        if draft and len(section["citations"]) == 0:
            findings.append(
                {
                    "severity": "MAJOR",
                    "area": "Evidence",
                    "section_id": section["section_id"],
                    "factor_ids": [],
                    "finding": "Draft has no structured citations.",
                    "suggested_fix": "Add citable proof or rewrite factual claims.",
                }
            )
        vague_hits = sorted(_tokens(draft).intersection({"robust", "seamless", "world-class", "innovative", "comprehensive"}))
        if vague_hits:
            findings.append(
                {
                    "severity": "MINOR",
                    "area": "Proposal voice",
                    "section_id": section["section_id"],
                    "factor_ids": [],
                    "finding": "Generic evaluator language detected: " + ", ".join(vague_hits),
                    "suggested_fix": "Replace vague adjectives with concrete proof, metrics, or named mechanisms.",
                }
            )

    if not themes or not themes.get("themes"):
        findings.append(
            {
                "severity": "MAJOR",
                "area": "Win themes",
                "section_id": None,
                "factor_ids": [],
                "finding": "No win themes are generated.",
                "suggested_fix": "Generate win themes and ensure major sections echo them.",
            }
        )
    if not pp or not pp.get("top_citable_projects"):
        findings.append(
            {
                "severity": "MAJOR",
                "area": "Past performance",
                "section_id": None,
                "factor_ids": [],
                "finding": "No citable past performance match is selected.",
                "suggested_fix": "Generate past performance matches and cite only won/subbed sources.",
            }
        )
    if not price or price.get("status") != "ready":
        findings.append(
            {
                "severity": "MINOR",
                "area": "Price-to-win",
                "section_id": None,
                "factor_ids": [],
                "finding": "Price-to-win posture is not ready.",
                "suggested_fix": "Run the cost flow and generate price-to-win before final cost narrative.",
            }
        )

    severity_order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    findings.sort(key=lambda f: (severity_order.get(f["severity"], 9), f.get("section_id") or "", f["area"]))
    payload = {
        "generated_at": _now_iso(),
        "proposal_id": proposal_id,
        "summary": {
            "critical": sum(1 for f in findings if f["severity"] == "CRITICAL"),
            "major": sum(1 for f in findings if f["severity"] == "MAJOR"),
            "minor": sum(1 for f in findings if f["severity"] == "MINOR"),
        },
        "findings": findings,
    }
    return _persist(proposal_id, "red_team_findings", payload)


def generate_graphics_tables(proposal_id: int) -> dict[str, Any]:
    snap = _snapshot(proposal_id)
    persisted = snap["persisted"]
    scorecard = persisted.get("evaluator_scorecard") or generate_evaluator_scorecard(proposal_id)
    themes = persisted.get("win_themes") or generate_win_themes(proposal_id)
    pp = persisted.get("past_performance_matches") or generate_past_performance_matches(proposal_id)
    price = persisted.get("price_to_win") or generate_price_to_win(proposal_id)

    section_by_req: dict[str, list[str]] = {}
    for section in snap["sections"]:
        for req_id in section["compliance_items"]:
            section_by_req.setdefault(req_id, []).append(section["section_id"])

    artifacts = [
        {
            "id": "GT-001",
            "title": "Evaluation Factor Readiness Scorecard",
            "type": "table",
            "recommended_placement": "Executive Summary or compliance appendix",
            "columns": ["Factor", "Readiness", "Score", "Mapped Sections", "Open Risks"],
            "rows": [
                {
                    "Factor": f"{f['factor_id']} {f['factor_name']}",
                    "Readiness": f["readiness_band"],
                    "Score": f["score"],
                    "Mapped Sections": ", ".join(f.get("mapped_section_ids") or []),
                    "Open Risks": "; ".join(f.get("evaluation_risks") or [])[:240],
                }
                for f in scorecard.get("factors") or []
            ],
        },
        {
            "id": "GT-002",
            "title": "Compliance-to-Solution Traceability Matrix",
            "type": "table",
            "recommended_placement": "Compliance appendix or technical approach",
            "columns": ["Requirement", "Category", "Response Section", "Status"],
            "rows": [
                {
                    "Requirement": item["requirement_id"],
                    "Category": item["category"],
                    "Response Section": ", ".join(section_by_req.get(item["requirement_id"], [])) or "Unassigned",
                    "Status": "Assigned" if section_by_req.get(item["requirement_id"]) else "Needs assignment",
                }
                for item in snap["compliance"][:80]
            ],
        },
        {
            "id": "GT-003",
            "title": "Win Theme Proof Map",
            "type": "table",
            "recommended_placement": "Executive Summary planning view",
            "columns": ["Theme", "Buyer Pain", "Proof Points", "Sections"],
            "rows": [
                {
                    "Theme": t["title"],
                    "Buyer Pain": t["buyer_pain"],
                    "Proof Points": "; ".join(t.get("proof_points") or []),
                    "Sections": ", ".join(t.get("section_targets") or []),
                }
                for t in themes.get("themes") or []
            ],
        },
        {
            "id": "GT-004",
            "title": "Past Performance Relevance Table",
            "type": "table",
            "recommended_placement": "Past Performance section",
            "columns": ["Project", "Customer", "Fit", "Matched Terms", "Citation Class"],
            "rows": [
                {
                    "Project": m.get("project"),
                    "Customer": m.get("customer"),
                    "Fit": f"{m.get('fit')} ({m.get('fit_score')})",
                    "Matched Terms": ", ".join(m.get("matched_terms") or []),
                    "Citation Class": m.get("citation_class"),
                }
                for m in (pp.get("matches") or [])[:5]
            ],
        },
        {
            "id": "GT-005",
            "title": "Price Scenario Comparison",
            "type": "table",
            "recommended_placement": "Cost volume narrative",
            "columns": ["Scenario", "Price", "Market Position", "Recommendation", "Narrative Role"],
            "rows": [
                {
                    "Scenario": s["scenario"],
                    "Price": f"${s['total_proposed_price']:,.0f}" if s.get("total_proposed_price") else "TBD",
                    "Market Position": s.get("vs_market_position") or "unknown",
                    "Recommendation": s.get("bid_recommendation") or "unknown",
                    "Narrative Role": s.get("narrative_role") or "comparison",
                }
                for s in price.get("scenarios") or []
            ],
        },
        {
            "id": "GT-006",
            "title": "Risk and Mitigation Register",
            "type": "table",
            "recommended_placement": "Risk or management approach",
            "columns": ["Gap", "Requirement", "Severity", "Mitigation", "Owner"],
            "rows": [
                {
                    "Gap": g["gap_id"],
                    "Requirement": g["req_id"],
                    "Severity": g["severity"],
                    "Mitigation": _chosen_gap_summary(g),
                    "Owner": g.get("selected_partner_name") or "Quadratic",
                }
                for g in snap["gaps"][:40]
            ],
        },
        {
            "id": "GT-007",
            "title": "Staffing Matrix",
            "type": "table",
            "recommended_placement": "Management approach or staffing plan",
            "columns": ["Role", "Person", "Labor Category", "Allocation", "Evidence"],
            "rows": [
                {
                    "Role": t.get("role_name"),
                    "Person": t.get("assigned_person") or t.get("person_kind") or "TBD",
                    "Labor Category": t.get("labor_category") or "TBD",
                    "Allocation": f"{t.get('time_allocation_pct')}%" if t.get("time_allocation_pct") is not None else "TBD",
                    "Evidence": (t.get("bio_summary") or "")[:160],
                }
                for t in snap["team"]
            ],
        },
    ]
    payload = {
        "generated_at": _now_iso(),
        "proposal_id": proposal_id,
        "artifacts": artifacts,
    }
    return _persist(proposal_id, "graphics_tables", payload)


def _chosen_gap_summary(gap: dict[str, Any]) -> str:
    opts = gap.get("mitigation_options") or []
    idx = gap.get("selected_mitigation_index")
    if idx is None:
        idx = gap.get("recommended_mitigation_index")
    if idx is None or idx < 0 or idx >= len(opts):
        return "No mitigation selected"
    opt = opts[idx]
    return (opt.get("approach") or opt.get("proposal_language_draft") or "Mitigation selected")[:180]


def generate_all_win_strategy(proposal_id: int) -> dict[str, Any]:
    """Refresh all strategy artifacts in dependency order."""
    return {
        "evaluator_scorecard": generate_evaluator_scorecard(proposal_id),
        "win_themes": generate_win_themes(proposal_id),
        "past_performance_matches": generate_past_performance_matches(proposal_id),
        "price_to_win": generate_price_to_win(proposal_id),
        "red_team_findings": generate_red_team_findings(proposal_id),
        "graphics_tables": generate_graphics_tables(proposal_id),
    }


def format_win_strategy_block_for_writer(proposal_id: int) -> str:
    """Render generated strategy artifacts into the Writer Team prefix."""
    strategy = load_win_strategy(proposal_id)
    blocks: list[str] = []
    scorecard = strategy.get("evaluator_scorecard")
    if scorecard:
        lines = [
            "=== EVALUATOR SCORECARD / SOURCE-SELECTION SIMULATION ===",
            f"Overall readiness: {scorecard.get('overall_readiness')} ({scorecard.get('overall_score')})",
        ]
        for factor in (scorecard.get("factors") or [])[:8]:
            lines.append(
                f"- {factor['factor_id']} {factor['factor_name']}: "
                f"{factor['readiness_band']} score={factor['score']}; "
                f"sections={', '.join(factor.get('mapped_section_ids') or []) or 'unmapped'}; "
                f"risks={'; '.join(factor.get('evaluation_risks') or [])[:240]}"
            )
        blocks.append("\n".join(lines))

    themes = strategy.get("win_themes")
    if themes and themes.get("themes"):
        lines = ["=== APPROVED WIN THEMES ==="]
        for theme in themes["themes"]:
            lines.append(
                f"- {theme['id']} {theme['title']}: {theme['discriminator']} "
                f"Proof: {'; '.join(theme.get('proof_points') or [])}"
            )
        blocks.append("\n".join(lines))

    pp = strategy.get("past_performance_matches")
    if pp and pp.get("top_citable_projects"):
        lines = ["=== PAST PERFORMANCE MATCH PRIORITY ==="]
        for match in pp["top_citable_projects"][:3]:
            lines.append(
                f"- {match.get('project')} ({match.get('customer')}): "
                f"{match.get('fit')} fit, cite as {match.get('citation_class')}; "
                f"matched terms={', '.join(match.get('matched_terms') or [])}"
            )
        blocks.append("\n".join(lines))

    price = strategy.get("price_to_win")
    if price:
        blocks.append(
            "\n".join(
                [
                    "=== PRICE-TO-WIN POSTURE ===",
                    f"Method: {price.get('source_selection_method')} / {price.get('posture')}",
                    f"Recommended scenario: {price.get('recommended_scenario')}",
                    f"Rationale: {price.get('rationale')}",
                    "Guardrails: " + "; ".join(price.get("guardrails") or []),
                ]
            )
        )

    red_team = strategy.get("red_team_findings")
    if red_team and red_team.get("findings"):
        lines = ["=== RED TEAM WATCH ITEMS ==="]
        for finding in red_team["findings"][:10]:
            lines.append(
                f"- [{finding.get('severity')}] {finding.get('area')} "
                f"{finding.get('section_id') or ''}: {finding.get('finding')} "
                f"Fix: {finding.get('suggested_fix')}"
            )
        blocks.append("\n".join(lines))

    graphics = strategy.get("graphics_tables")
    if graphics and graphics.get("artifacts"):
        lines = ["=== RECOMMENDED TABLES / VISUAL ARTIFACTS ==="]
        for artifact in graphics["artifacts"][:7]:
            lines.append(
                f"- {artifact['id']} {artifact['title']} "
                f"({artifact['recommended_placement']})"
            )
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return "\n\n".join(blocks)
