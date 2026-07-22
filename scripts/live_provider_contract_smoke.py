"""Bounded, non-mutating smoke test for the configured LLM providers.

The probe intentionally uses ``proposal_id=None`` so the production wrappers
do not write AgentRun rows.  It prints model names, token counts, cost, and
pass/fail only; prompts, provider responses, and credentials are not printed.

Usage::

    python scripts/live_provider_contract_smoke.py
    python scripts/live_provider_contract_smoke.py --include-search
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from typing import Any

from app.config import get_settings
from app.services.llm import call_tool_for_model, get_anthropic, get_gemini

_PROBE_TOOL: dict[str, Any] = {
    "name": "provider_contract_probe",
    "description": "Return the requested provider contract acknowledgement.",
    "input_schema": {
        "type": "object",
        "properties": {
            "provider": {"type": "string"},
            "ok": {"type": "boolean"},
        },
        "required": ["provider", "ok"],
        "additionalProperties": False,
    },
}


def _usage_summary(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cost_usd": round(float(usage.get("cost_usd") or 0.0), 6),
    }


def _run_check(name: str, model: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        details = fn()
        return {
            "name": name,
            "model": model,
            "ok": True,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            **details,
        }
    except Exception as exc:
        return {
            "name": name,
            "model": model,
            "ok": False,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }


def _tool_probe(provider: str, model: str) -> dict[str, Any]:
    result, usage = call_tool_for_model(
        model=model,
        system="Follow the user's instruction and call the required tool.",
        messages=[{
            "role": "user",
            "content": (
                "Call provider_contract_probe with provider exactly "
                f"{provider!r} and ok=true."
            ),
        }],
        tool=_PROBE_TOOL,
        max_tokens=256,
        agent_name=f"live_contract_{provider}",
        proposal_id=None,
        temperature=0.0,
    )
    if result.get("provider") != provider or result.get("ok") is not True:
        raise AssertionError("provider returned a structurally valid but incorrect acknowledgement")
    return _usage_summary(usage)


def _anthropic_text_probe(model: str) -> dict[str, Any]:
    text, usage = get_anthropic().complete(
        model=model,
        system="Answer with one word only.",
        messages=[{"role": "user", "content": "Reply READY."}],
        max_tokens=16,
        agent_name="live_contract_anthropic_text",
        proposal_id=None,
        temperature=0.0,
    )
    if not text.strip():
        raise AssertionError("Anthropic text completion returned no text")
    return _usage_summary(usage)


def _anthropic_search_probe(model: str) -> dict[str, Any]:
    text, citations, usage = get_anthropic().complete_with_web_search(
        model=model,
        system="Use web search and answer concisely.",
        user_prompt="Use web search to find the official NIST home page and return its URL.",
        max_tokens=256,
        max_uses=1,
        agent_name="live_contract_anthropic_search",
        proposal_id=None,
        temperature=0.0,
    )
    if not text.strip():
        raise AssertionError("Anthropic web-search completion returned no text")
    summary = _usage_summary(usage)
    summary["citations"] = len(citations)
    summary["web_searches"] = int(usage.get("web_searches") or 0)
    return summary


def _gemini_search_probe(model: str) -> dict[str, Any]:
    text, citations, usage = get_gemini().complete_with_search(
        model=model,
        system="Use Google Search and answer concisely.",
        user_prompt="Use Google Search to find the official NIST home page and return its URL.",
        max_tokens=256,
        agent_name="live_contract_gemini_search",
        proposal_id=None,
        temperature=0.0,
    )
    if not text.strip():
        raise AssertionError("Gemini grounded completion returned no text")
    summary = _usage_summary(usage)
    summary["citations"] = len(citations)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-search",
        action="store_true",
        help="also make one bounded grounded-search call to Anthropic and Gemini",
    )
    args = parser.parse_args()

    settings = get_settings()
    provider_models = {
        "anthropic": settings.model_light_extraction,
        "openai": settings.model_reviewer_a,
        "gemini": settings.model_reviewer_b,
    }
    missing = [
        name for name, present in {
            "anthropic": bool(settings.anthropic_api_key),
            "openai": bool(settings.openai_api_key),
            "gemini": bool(settings.google_or_gemini_key),
        }.items() if not present
    ]
    if missing:
        print(json.dumps({"ok": False, "missing_credentials": missing}, indent=2))
        return 2

    checks: list[dict[str, Any]] = []
    for provider, model in provider_models.items():
        checks.append(_run_check(
            f"{provider}_forced_tool",
            model,
            lambda provider=provider, model=model: _tool_probe(provider, model),
        ))
    checks.append(_run_check(
        "anthropic_text_completion",
        provider_models["anthropic"],
        lambda: _anthropic_text_probe(provider_models["anthropic"]),
    ))

    if args.include_search:
        checks.append(_run_check(
            "anthropic_web_search",
            settings.model_teaming_researcher_b,
            lambda: _anthropic_search_probe(settings.model_teaming_researcher_b),
        ))
        checks.append(_run_check(
            "gemini_grounded_search",
            settings.model_teaming_researcher,
            lambda: _gemini_search_probe(settings.model_teaming_researcher),
        ))

    total_cost = round(sum(float(check.get("cost_usd") or 0.0) for check in checks), 6)
    payload = {
        "ok": all(check["ok"] for check in checks),
        "non_mutating": True,
        "checks": checks,
        "estimated_total_cost_usd": total_cost,
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
