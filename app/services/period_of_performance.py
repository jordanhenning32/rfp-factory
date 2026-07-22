"""Period-of-performance extraction helpers.

The cost pipeline used to assume 12 months everywhere. That is a
reasonable fallback, but it badly overprices short state/local
consulting RFQs where the solicitation says "6 months" or gives a
specific start/end date range in a pricing table.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.db.session import session_scope
from app.models import ComplianceMatrixItem, Proposal, RfpPackageDocument

DEFAULT_POP_MONTHS = 12

_MONTH_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "eighteen": 18,
    "twenty four": 24,
    "twenty-four": 24,
    "thirty six": 36,
    "thirty-six": 36,
}

_DURATION_RE = re.compile(
    r"\b(?P<n>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|eighteen|twenty[- ]four|thirty[- ]six)\s*"
    r"(?:-| )?(?:month|months|mo\.?|mos\.?)\b",
    re.IGNORECASE,
)

_CONTRACT_KEYWORDS_RE = re.compile(
    r"\b(period of performance|performance period|term|contract|project|"
    r"effective date|commence|commencement|expire|expires|expiration|"
    r"calendar year|base period|option year|fee for consulting)\b",
    re.IGNORECASE,
)

_NUMERIC_DATE_RE = (
    r"(?P<m>\d{1,2})[/-](?P<d>\d{1,2})[/-](?P<y>\d{2,4})"
)
_NUMERIC_RANGE_RE = re.compile(
    _NUMERIC_DATE_RE
    + r"\s*(?:-|--|to|through|thru|until)\s*"
    + _NUMERIC_DATE_RE.replace("?P<", "?P<e_"),
    re.IGNORECASE,
)

_MONTH_NAME = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
_NAMED_DATE_RE = (
    rf"(?P<mon>{_MONTH_NAME})\s+"
    r"(?P<d>\d{1,2})(?:st|nd|rd|th)?[,]?\s+"
    r"(?P<y>\d{4})"
)
_NAMED_RANGE_RE = re.compile(
    _NAMED_DATE_RE
    + r"\s*(?:-|--|to|through|thru|until)\s*"
    + _NAMED_DATE_RE.replace("?P<", "?P<e_"),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PeriodOfPerformanceEstimate:
    months: int
    source: str
    confidence: str


def detect_pop_months_from_text(
    text: str,
    *,
    default: int = DEFAULT_POP_MONTHS,
) -> PeriodOfPerformanceEstimate:
    """Infer the contract period in months from solicitation text.

    The extractor deliberately favors contract-duration wording over
    arbitrary dates. Date ranges are accepted when they look like a
    start/end range, such as a cost table line.
    """
    text = (text or "").strip()
    if not text:
        return PeriodOfPerformanceEstimate(
            months=default,
            source="default",
            confidence="default",
        )

    # Duration phrases are the cleanest signal: "expires 6 months
    # after the Effective Date", "6-month performance period", etc.
    for match in _DURATION_RE.finditer(text):
        window = text[max(0, match.start() - 120):match.end() + 120]
        if _CONTRACT_KEYWORDS_RE.search(window):
            n = _duration_value(match.group("n"))
            if n is not None and 1 <= n <= 120:
                return PeriodOfPerformanceEstimate(
                    months=n,
                    source=_clean_source(window),
                    confidence="high",
                )

    # Numeric date ranges catch compact pricing tables:
    # "7/1/2026 - 12/31/2026 | 1 EACH".
    for match in _NUMERIC_RANGE_RE.finditer(text):
        start = _parse_numeric_date(
            match.group("m"), match.group("d"), match.group("y"),
        )
        end = _parse_numeric_date(
            match.group("e_m"), match.group("e_d"), match.group("e_y"),
        )
        months = _inclusive_month_span(start, end)
        if months is not None:
            window = text[max(0, match.start() - 80):match.end() + 80]
            return PeriodOfPerformanceEstimate(
                months=months,
                source=_clean_source(window),
                confidence=(
                    "high" if _CONTRACT_KEYWORDS_RE.search(window)
                    else "medium"
                ),
            )

    # Named date ranges: "July 1, 2026 through December 31, 2026".
    for match in _NAMED_RANGE_RE.finditer(text):
        start = _parse_named_date(
            match.group("mon"), match.group("d"), match.group("y"),
        )
        end = _parse_named_date(
            match.group("e_mon"), match.group("e_d"), match.group("e_y"),
        )
        months = _inclusive_month_span(start, end)
        if months is not None:
            window = text[max(0, match.start() - 80):match.end() + 80]
            return PeriodOfPerformanceEstimate(
                months=months,
                source=_clean_source(window),
                confidence=(
                    "high" if _CONTRACT_KEYWORDS_RE.search(window)
                    else "medium"
                ),
            )

    return PeriodOfPerformanceEstimate(
        months=default,
        source="default",
        confidence="default",
    )


def detect_pop_months_for_proposal(
    proposal_id: int,
    *,
    default: int = DEFAULT_POP_MONTHS,
) -> PeriodOfPerformanceEstimate:
    """Read a proposal's compliance/doc text and infer PoP months."""
    chunks: list[str] = []
    with session_scope() as db:
        rows = db.execute(
            select(ComplianceMatrixItem.requirement_text)
            .where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
            .order_by(ComplianceMatrixItem.id)
        ).scalars().all()
        chunks.extend(t for t in rows if t)

        if not chunks:
            prop = db.get(Proposal, proposal_id)
            if prop is not None:
                docs = db.execute(
                    select(RfpPackageDocument.extracted_text_md)
                    .where(RfpPackageDocument.rfp_package_id == prop.rfp_package_id)
                    .limit(2)
                ).scalars().all()
                chunks.extend((d or "")[:8000] for d in docs if d)

    best = PeriodOfPerformanceEstimate(
        months=default,
        source="default",
        confidence="default",
    )
    rank = {"default": 0, "medium": 1, "high": 2}
    for chunk in chunks:
        estimate = detect_pop_months_from_text(chunk, default=default)
        if rank[estimate.confidence] > rank[best.confidence]:
            best = estimate
            if best.confidence == "high":
                break
    return best


def get_pop_months_for_proposal(
    proposal_id: int,
    *,
    default: int = DEFAULT_POP_MONTHS,
) -> int:
    """Convenience wrapper for callers that only need the integer."""
    return detect_pop_months_for_proposal(
        proposal_id,
        default=default,
    ).months


def _duration_value(raw: str) -> int | None:
    s = (raw or "").strip().lower()
    if s.isdigit():
        return int(s)
    return _MONTH_WORDS.get(s)


def _parse_numeric_date(month: str, day: str, year: str) -> datetime | None:
    y = int(year)
    if y < 100:
        y += 2000
    try:
        return datetime(y, int(month), int(day))
    except ValueError:
        return None


def _parse_named_date(month: str, day: str, year: str) -> datetime | None:
    try:
        return datetime.strptime(
            f"{month} {int(day)} {year}",
            "%B %d %Y",
        )
    except ValueError:
        try:
            return datetime.strptime(
                f"{month} {int(day)} {year}",
                "%b %d %Y",
            )
        except ValueError:
            return None


def _inclusive_month_span(
    start: datetime | None,
    end: datetime | None,
) -> int | None:
    if start is None or end is None or end < start:
        return None
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    if months < 1 or months > 120:
        return None

    # A short partial month at the end should not count as a full
    # contract month, but month-end ranges such as Jul 1-Dec 31 should.
    last_day = calendar.monthrange(end.year, end.month)[1]
    if end.day < last_day and start.day == 1 and months > 1:
        months -= 1
    return max(1, months)


def _clean_source(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:240]


__all__ = [
    "DEFAULT_POP_MONTHS",
    "PeriodOfPerformanceEstimate",
    "detect_pop_months_for_proposal",
    "detect_pop_months_from_text",
    "get_pop_months_for_proposal",
]
