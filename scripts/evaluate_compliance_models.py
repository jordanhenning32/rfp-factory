"""Opt-in live comparison of requirements-review models against the gold set.

Examples:
    .venv/Scripts/python scripts/evaluate_compliance_models.py --live \
        --models gemini-2.5-pro claude-haiku-4-5-20251001 --runs 3

Normal regression tests never execute provider calls. ``--live`` is required
so this evaluation cannot incur API cost accidentally.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents import compliance_completeness as completeness  # noqa: E402
from app.agents import compliance_validator as validator  # noqa: E402
from app.evals.compliance_review_gold import (  # noqa: E402
    load_gold_set,
    score_model_outputs,
)
from app.services.llm import call_tool_for_model  # noqa: E402


def _usage_cost(usage: dict[str, Any] | None) -> float:
    return float((usage or {}).get("cost_usd") or 0.0)


def _agent_suffix(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:40]


def _evaluate_case(model: str, case: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    output: dict[str, Any] = {
        "findings": [],
        "missing_candidates": [],
        "protocol_ok": True,
        "cost_usd": 0.0,
    }
    suffix = _agent_suffix(model)
    items = list(case["items"])
    try:
        if items:
            prompt = validator._USER_TEMPLATE.format(
                n=len(items),
                items_text=validator._format_items_for_validation(items),
            )
            payload, usage = call_tool_for_model(
                model=model,
                system=validator._SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tool=validator._TOOL,
                max_tokens=4000,
                agent_name=f"compliance_gold_classification_{suffix}",
                proposal_id=None,
            )
            output["findings"] = [
                asdict(item)
                for item in validator._parse_results_strict(payload, items)
            ]
            output["cost_usd"] += _usage_cost(usage)

        source_units = completeness._source_units(case["source_text"])
        for unit in source_units:
            prompt = completeness._USER_TEMPLATE.format(
                filename=f"gold/{case['id']}",
                unit_label=unit.label,
                items_text=completeness._format_items(items, unit),
                source_text=unit.text,
            )
            payload, usage = call_tool_for_model(
                model=model,
                system=completeness._SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tool=completeness._TOOL,
                max_tokens=6000,
                agent_name=f"compliance_gold_completeness_{suffix}",
                proposal_id=None,
            )
            candidates, _uncertain, _ignored = completeness._parse_payload_strict(
                payload,
                unit,
                items,
            )
            output["missing_candidates"].extend(
                asdict(candidate) for candidate in candidates
            )
            output["cost_usd"] += _usage_cost(usage)
    except Exception as exc:
        output["protocol_ok"] = False
        output["error_kind"] = type(exc).__name__
    output["latency_seconds"] = time.perf_counter() - started
    return output


def _evaluate_model(model: str, gold: dict[str, Any]) -> dict[str, Any]:
    outputs = {
        case["id"]: _evaluate_case(model, case)
        for case in gold["cases"]
    }
    return {
        "model": model,
        "metrics": score_model_outputs(gold, outputs),
        "outputs": outputs,
    }


def _mean_metrics(runs: list[dict[str, Any]]) -> dict[str, float]:
    names = runs[0]["metrics"].keys()
    return {
        name: round(mean(float(run["metrics"][name]) for run in runs), 6)
        for name in names
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="authorize live API calls")
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--gold", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required because this command incurs provider cost")
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    gold = load_gold_set(args.gold)
    results: dict[str, Any] = {
        "gold_version": gold["version"],
        "case_count": len(gold["cases"]),
        "models": {},
    }
    for model in args.models:
        runs = [_evaluate_model(model, gold) for _ in range(args.runs)]
        results["models"][model] = {
            "runs": runs,
            "mean_metrics": _mean_metrics(runs),
        }

    rendered = json.dumps(results, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
