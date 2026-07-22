from app.services.period_of_performance import detect_pop_months_from_text
from app.services.pricing import (
    CostAnalystLaborLine,
    CostAnalystOutput,
    apply_cost_build_edits_to_output,
)


def test_detects_pmrs_numeric_date_range_as_six_months():
    estimate = detect_pop_months_from_text(
        "Calendar Year | Quantity | Fee for Consulting & Performance Measurement\n"
        "7/1/2026 - 12/31/2026 | 1 EACH"
    )

    assert estimate.months == 6
    assert estimate.confidence == "high"


def test_detects_contract_term_duration_as_high_confidence():
    estimate = detect_pop_months_from_text(
        'The term of this Project shall commence upon issuance of a Contract '
        'or Purchase Order to the selected Contractor ("Effective Date") and '
        "shall expire 6 months after the Effective Date."
    )

    assert estimate.months == 6
    assert estimate.confidence == "high"


def test_cost_edits_are_keyed_by_line_index_not_category():
    output = CostAnalystOutput(
        labor_lines=[
            CostAnalystLaborLine(
                labor_category="Program Director",
                wage_band="230k",
                hours=100,
                rationale="first",
            ),
            CostAnalystLaborLine(
                labor_category="Program Director",
                wage_band="230k",
                hours=200,
                rationale="second",
            ),
        ],
        avg_headcount_during_pop=4,
    )

    edited = apply_cost_build_edits_to_output(
        output,
        labor_edits={
            "0": {"hours": 50},
            "1": {"hours": 80, "wage_band": "190k"},
        },
    )

    assert edited.labor_lines[0].hours == 50
    assert edited.labor_lines[0].wage_band == "230k"
    assert edited.labor_lines[1].hours == 80
    assert edited.labor_lines[1].wage_band == "190k"
