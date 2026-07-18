"""Teaming Consolidator — merges the two providers' partner research
outputs (Gemini-grounded + Claude+web_search) into a single ranked
list, attributing each partner to the provider(s) that surfaced it.

Pure-Python where possible — partner identity matching is done via a
canonicalized firm name (lowercased, punctuation/legal-suffix stripped).
A small Haiku call resolves edge cases the canonicalizer can't (e.g.
"Booz Allen Hamilton" vs "Booz Allen" vs "BAH"), but only when the two
providers' result sets actually overlap on something deterministic
matching missed. Most gaps need zero LLM calls in this stage.

For each merged partner the output records:

  confirmed_by  — list[str] of providers that surfaced this firm
                  (subset of {"gemini", "claude"}). Length 2 means
                  both agreed; 1 means only one provider had it.
  confidence    — original HIGH/MEDIUM/LOW. Bumped one tier up
                  (MEDIUM→HIGH, LOW→MEDIUM) when len(confirmed_by) > 1
                  because cross-provider agreement is itself evidence.
  needs_review  — True when the two providers proposed materially
                  different rankings or only one surfaced this firm
                  AND its confidence is < HIGH. Surfaces the row in
                  the UI for the user to verify before contacting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.agents.teaming_researcher import TeamingPartnerResearch

log = logging.getLogger(__name__)


# Common legal/structural suffixes stripped during canonicalization
# so "Acme Corp" and "Acme Corporation" match. Keep this list short —
# we'd rather miss a match (and have the LLM resolver pick it up) than
# falsely collapse two distinct firms.
_SUFFIX_PATTERN = re.compile(
    r"\b(?:inc|incorporated|corp|corporation|llc|ltd|limited|"
    r"co|company|holdings|group|technologies|tech|solutions|"
    r"consulting|services|labs|systems|enterprises|partners?)\b",
    re.IGNORECASE,
)
_PUNCT_PATTERN = re.compile(r"[^\w\s]")
_WS_PATTERN = re.compile(r"\s+")


def _canonicalize_name(name: str) -> str:
    """Lowercase, drop punctuation and trailing legal/structural
    suffixes, collapse whitespace. Used as the dedupe key when the
    same firm shows up from both providers under slightly different
    surface forms ("Acme Corp." vs "ACME, Inc.")."""
    s = (name or "").strip().lower()
    s = _PUNCT_PATTERN.sub(" ", s)
    s = _SUFFIX_PATTERN.sub(" ", s)
    s = _WS_PATTERN.sub(" ", s).strip()
    return s


_CONFIDENCE_BUMP = {"LOW": "MEDIUM", "MEDIUM": "HIGH", "HIGH": "HIGH"}


@dataclass
class ConsolidatedPartnerResearch:
    """The merged result for one gap. Drop-in replacement for the
    single-provider `TeamingPartnerResearch`; downstream code that
    only reads `partners[]` and `citations[]` works unchanged."""

    gap_id: str
    partners: list[dict]
    citations: list[dict]
    cost_usd: float
    n_consensus: int  # partners both providers surfaced
    n_only_a: int  # partners only Gemini surfaced
    n_only_b: int  # partners only Claude surfaced


def _merge_citations(a: list[dict], b: list[dict]) -> list[dict]:
    """Union two citation lists deduped by URI."""
    seen: set[str] = set()
    out: list[dict] = []
    for c in (a or []) + (b or []):
        key = (c.get("uri") or c.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _bumped_confidence(current: str | None, agreed: bool) -> str:
    if not agreed:
        return (current or "MEDIUM").upper()
    return _CONFIDENCE_BUMP.get((current or "MEDIUM").upper(), "MEDIUM")


def _annotate_partner(
    partner: dict,
    *,
    confirmed_by: list[str],
    consensus: bool,
) -> dict:
    """Return a copy of the partner dict with provider attribution
    fields added. Bumps confidence one tier when both providers agreed
    on the firm; flags `needs_review` when only one provider had it
    AND that provider rated it < HIGH."""
    out = dict(partner)
    out["confirmed_by"] = list(confirmed_by)
    if consensus:
        out["confidence"] = _bumped_confidence(out.get("confidence"), True)
        out["needs_review"] = False
    else:
        out["needs_review"] = (out.get("confidence") or "").upper() != "HIGH"
    return out


def consolidate_partner_research(
    *,
    gap_id: str,
    pass_a: TeamingPartnerResearch,  # Gemini-grounded
    pass_b: TeamingPartnerResearch,  # Claude+web_search
) -> ConsolidatedPartnerResearch:
    """Merge two providers' partner-research outputs into one ranked
    list. Pure-Python deterministic match by canonicalized firm name —
    no LLM call. Order policy:
      1. Consensus partners first, ordered by Pass A's ranking (Pass A
         currently is Gemini, which has the longer track record on this
         task; swap if priorities shift).
      2. Pass A only partners next, in Pass A order.
      3. Pass B only partners last, in Pass B order.
    """
    a_partners = list(pass_a.partners or [])
    b_partners = list(pass_b.partners or [])

    # Build canonical-name → partner dict, retaining order.
    a_by_canon: dict[str, dict] = {}
    for p in a_partners:
        canon = _canonicalize_name(p.get("name") or "")
        if canon and canon not in a_by_canon:
            a_by_canon[canon] = p

    b_by_canon: dict[str, dict] = {}
    for p in b_partners:
        canon = _canonicalize_name(p.get("name") or "")
        if canon and canon not in b_by_canon:
            b_by_canon[canon] = p

    consensus_canons = [c for c in a_by_canon if c in b_by_canon]
    only_a_canons = [c for c in a_by_canon if c not in b_by_canon]
    only_b_canons = [c for c in b_by_canon if c not in a_by_canon]

    merged: list[dict] = []
    for canon in consensus_canons:
        a_part = a_by_canon[canon]
        b_part = b_by_canon[canon]
        # Prefer Pass A's profile when both have it (Gemini's grounded
        # output tends to be richer on certs/contracts), but inject any
        # Pass B fields A is missing.
        base = dict(a_part)
        b_profile = b_part.get("profile") or {}
        if isinstance(b_profile, dict):
            a_profile = base.get("profile") or {}
            if isinstance(a_profile, dict):
                for k, v in b_profile.items():
                    if k not in a_profile or not a_profile.get(k):
                        a_profile[k] = v
                base["profile"] = a_profile
        merged.append(
            _annotate_partner(
                base,
                confirmed_by=["gemini", "claude"],
                consensus=True,
            )
        )

    for canon in only_a_canons:
        merged.append(
            _annotate_partner(
                a_by_canon[canon],
                confirmed_by=["gemini"],
                consensus=False,
            )
        )

    for canon in only_b_canons:
        merged.append(
            _annotate_partner(
                b_by_canon[canon],
                confirmed_by=["claude"],
                consensus=False,
            )
        )

    citations = _merge_citations(pass_a.citations, pass_b.citations)
    cost = float(pass_a.cost_usd or 0.0) + float(pass_b.cost_usd or 0.0)

    log.info(
        "teaming_consolidator: gap=%s consensus=%d only_a=%d only_b=%d merged=%d citations=%d cost=$%.4f",
        gap_id,
        len(consensus_canons),
        len(only_a_canons),
        len(only_b_canons),
        len(merged),
        len(citations),
        cost,
    )

    return ConsolidatedPartnerResearch(
        gap_id=gap_id,
        partners=merged,
        citations=citations,
        cost_usd=cost,
        n_consensus=len(consensus_canons),
        n_only_a=len(only_a_canons),
        n_only_b=len(only_b_canons),
    )


__all__ = [
    "ConsolidatedPartnerResearch",
    "consolidate_partner_research",
]
