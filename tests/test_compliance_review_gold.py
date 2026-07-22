from __future__ import annotations

from typing import Any


def _missing_candidate(expectation: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(expectation, str):
        return {"requirement_text": expectation}
    return dict(expectation)


def _perfect_outputs(gold: dict[str, Any]) -> dict[str, dict[str, Any]]:
    outputs = {}
    for case in gold["cases"]:
        expected = case["expected"]
        outputs[case["id"]] = {
            "protocol_ok": True,
            "findings": [
                {**finding, "confidence": "HIGH"}
                for finding in expected["findings"]
            ],
            "missing_candidates": [
                _missing_candidate(missing)
                for missing in expected["missing_requirements"]
            ],
        }
    return outputs


def _passing_scores() -> dict[str, float | int]:
    return {
        "critical_misses": 0,
        "protocol_success_rate": 1.0,
        "finding_f1": 1.0,
        "finding_precision": 1.0,
        "classification_accuracy": 1.0,
        "missing_requirement_precision": 1.0,
        "missing_requirement_recall": 1.0,
        "missing_requirement_f1": 1.0,
        "missing_metadata_accuracy": 1.0,
        "high_auto_fix_precision": 1.0,
        "high_missing_auto_add_precision": 1.0,
    }


def test_gold_set_is_versioned_synthetic_and_covers_key_risks() -> None:
    from app.evals.compliance_review_gold import load_gold_set

    gold = load_gold_set()
    ids = {case["id"] for case in gold["cases"]}

    assert gold["version"] == "1.0"
    assert len(ids) >= 12
    assert "mandatory_verb_misclassified" in ids
    assert "bare_imperative_parent_context" in ids
    assert "intentional_omission_certification" in ids
    assert "pricing_omission" in ids
    omissions = [
        missing
        for case in gold["cases"]
        for missing in case["expected"]["missing_requirements"]
    ]
    assert omissions
    assert all(isinstance(missing, dict) for missing in omissions)
    assert all(
        {
            "requirement_text",
            "source_page",
            "requirement_type",
            "category",
            "weight",
            "confidence",
        }
        <= set(missing)
        for missing in omissions
    )


def test_scorer_reports_perfect_metrics_for_gold_outputs() -> None:
    from app.evals.compliance_review_gold import load_gold_set, score_model_outputs

    gold = load_gold_set()
    outputs = _perfect_outputs(gold)

    metrics = score_model_outputs(gold, outputs)

    assert metrics["protocol_success_rate"] == 1.0
    assert metrics["finding_precision"] == 1.0
    assert metrics["finding_recall"] == 1.0
    assert metrics["finding_f1"] == 1.0
    assert metrics["missing_requirement_precision"] == 1.0
    assert metrics["missing_requirement_recall"] == 1.0
    assert metrics["missing_requirement_f1"] == 1.0
    assert metrics["missing_requirement_false_positives"] == 0
    assert metrics["missing_metadata_accuracy"] == 1.0
    assert metrics["missing_metadata_evaluated"] == 2
    assert metrics["high_missing_auto_add_precision"] == 1.0
    assert metrics["critical_misses"] == 0


def test_candidate_gate_rejects_cheap_model_with_critical_miss() -> None:
    from app.evals.compliance_review_gold import candidate_meets_replacement_gate

    primary = _passing_scores()
    candidate = dict(primary)
    candidate["critical_misses"] = 1

    assert not candidate_meets_replacement_gate(primary, candidate)


def test_candidate_gate_rejects_wrong_classification_suggestions() -> None:
    from app.evals.compliance_review_gold import candidate_meets_replacement_gate

    primary = _passing_scores()
    candidate = dict(primary, classification_accuracy=0.0)

    assert not candidate_meets_replacement_gate(primary, candidate)


def test_candidate_gate_rejects_bogus_omissions_despite_full_recall() -> None:
    from app.evals.compliance_review_gold import (
        candidate_meets_replacement_gate,
        load_gold_set,
        score_model_outputs,
    )

    gold = load_gold_set()
    outputs = _perfect_outputs(gold)

    primary = score_model_outputs(gold, outputs)
    noisy_outputs = {
        case_id: {
            **output,
            "missing_candidates": list(output["missing_candidates"]),
        }
        for case_id, output in outputs.items()
    }
    first_case_id = str(gold["cases"][0]["id"])
    noisy_outputs[first_case_id]["missing_candidates"].append(
        {"requirement_text": "This fabricated omission does not appear in the gold set."}
    )
    noisy_candidate = score_model_outputs(gold, noisy_outputs)

    assert noisy_candidate["missing_requirement_recall"] == 1.0
    assert noisy_candidate["missing_requirement_precision"] < 1.0
    assert noisy_candidate["missing_requirement_false_positives"] == 1
    assert not candidate_meets_replacement_gate(primary, noisy_candidate)


def test_candidate_gate_fails_closed_without_omission_precision() -> None:
    from app.evals.compliance_review_gold import candidate_meets_replacement_gate

    legacy_scores = {
        "critical_misses": 0,
        "protocol_success_rate": 1.0,
        "finding_f1": 1.0,
        "finding_precision": 1.0,
        "missing_requirement_recall": 1.0,
        "high_auto_fix_precision": 1.0,
    }

    assert not candidate_meets_replacement_gate(legacy_scores, legacy_scores)


def test_string_omission_expectations_remain_backward_compatible() -> None:
    from app.evals.compliance_review_gold import score_model_outputs

    gold = {
        "version": "legacy",
        "cases": [
            {
                "id": "legacy-string",
                "source_text": "The offeror shall submit a plan.",
                "items": [],
                "expected": {
                    "findings": [],
                    "missing_requirements": [
                        "The offeror shall submit a plan."
                    ],
                    "critical_missing": True,
                },
            }
        ],
    }
    outputs = {
        "legacy-string": {
            "protocol_ok": True,
            "missing_candidates": [
                {
                    "requirement_text": "The offeror shall submit a plan.",
                    "confidence": "HIGH",
                }
            ],
        }
    }

    metrics = score_model_outputs(gold, outputs)

    assert metrics["missing_requirement_f1"] == 1.0
    assert metrics["missing_metadata_evaluated"] == 0
    assert metrics["missing_metadata_accuracy"] == 1.0
    assert metrics["high_missing_auto_add_precision"] == 1.0
    assert metrics["critical_misses"] == 0


def test_candidate_gate_rejects_right_omission_text_with_wrong_metadata() -> None:
    from app.evals.compliance_review_gold import (
        candidate_meets_replacement_gate,
        load_gold_set,
        score_model_outputs,
    )

    gold = load_gold_set()
    primary_outputs = _perfect_outputs(gold)
    candidate_outputs = _perfect_outputs(gold)
    candidate = candidate_outputs["intentional_omission_certification"]
    candidate["missing_candidates"][0]["category"] = "technical"

    primary_scores = score_model_outputs(gold, primary_outputs)
    candidate_scores = score_model_outputs(gold, candidate_outputs)

    assert candidate_scores["missing_requirement_precision"] == 1.0
    assert candidate_scores["missing_requirement_recall"] == 1.0
    assert candidate_scores["missing_metadata_accuracy"] < 1.0
    assert candidate_scores["high_missing_auto_add_precision"] < 1.0
    assert not candidate_meets_replacement_gate(primary_scores, candidate_scores)


def test_candidate_gate_rejects_fractional_critical_miss_mean() -> None:
    from app.evals.compliance_review_gold import candidate_meets_replacement_gate

    primary = _passing_scores()
    one_miss_across_three_runs = dict(primary, critical_misses=1 / 3)

    assert not candidate_meets_replacement_gate(primary, one_miss_across_three_runs)


def test_candidate_gate_accepts_fully_passing_metadata_scores() -> None:
    from app.evals.compliance_review_gold import candidate_meets_replacement_gate

    scores = _passing_scores()

    assert candidate_meets_replacement_gate(scores, scores)
