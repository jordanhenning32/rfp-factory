"""Deterministic, test-only replacement for every supported LLM provider.

The E2E server imports this module and installs the replacement *before*
``app.main`` imports the UI, jobs, and agents. This keeps the production app
free of an environment-controlled fake-provider branch while ensuring an E2E
run can never spend money or depend on an external model API.

Fixture file format::

    {
      "responses": {
        "call_tool:compliance_matrix:extract_requirements": {
          "tool_input": {"items": [...]},
          "usage": {"input_tokens": 100, "output_tokens": 20}
        },
        "complete:intake_metadata": {
          "text": "{\"title\": \"Synthetic RFP\"}"
        },
        "complete_with_search:market_researcher_grounded": {
          "text": "Synthetic grounded brief",
          "citations": []
        }
      }
    }

Responses are keyed by method, agent name, and (for tool calls) tool name.
There is no call-order routing: production jobs fan out concurrently, so
sequence-based fixtures would be nondeterministic. An unknown request fails
closed with a descriptive error.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEFAULT_USAGE: dict[str, Any] = {
    "input_tokens": 100,
    "output_tokens": 25,
    "cached_input_tokens": 0,
    "cache_read_tokens": 0,
    "cost_usd": 0.0,
    "stop_reason": "tool_use",
}


class MissingFixtureError(RuntimeError):
    """Raised when production invokes an LLM interaction not in the registry."""


class FixtureLLM:
    """Provider-shaped deterministic client backed by a JSON response map."""

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        *,
        ledger_path: Path | None = None,
    ) -> None:
        self._responses = responses or {}
        self._ledger_path = ledger_path
        self._lock = threading.Lock()
        self._call_number = 0

    @classmethod
    def from_environment(cls) -> FixtureLLM:
        fixture_value = os.environ.get("RFP_E2E_LLM_FIXTURES", "").strip()
        responses: dict[str, Any] = {}
        if fixture_value:
            fixture_path = Path(fixture_value).resolve()
            try:
                payload = json.loads(fixture_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Could not load E2E LLM fixtures from {fixture_path}: {exc}"
                ) from exc
            raw_responses = payload.get("responses", payload)
            if not isinstance(raw_responses, dict):
                raise RuntimeError(
                    f"E2E LLM fixture root must be an object: {fixture_path}"
                )
            responses = raw_responses

        ledger_value = os.environ.get("RFP_E2E_LLM_LEDGER", "").strip()
        ledger_path = Path(ledger_value).resolve() if ledger_value else None
        return cls(responses, ledger_path=ledger_path)

    def _lookup(
        self,
        method: str,
        *,
        agent_name: str,
        tool_name: str | None = None,
        model: str = "",
    ) -> dict[str, Any]:
        candidates = []
        if tool_name:
            candidates.append(f"{method}:{agent_name}:{tool_name}")
        candidates.append(f"{method}:{agent_name}")

        self._record_call(
            method=method,
            agent_name=agent_name,
            tool_name=tool_name,
            model=model,
        )
        for key in candidates:
            if key in self._responses:
                response = self._responses[key]
                if not isinstance(response, dict):
                    raise RuntimeError(f"E2E LLM fixture {key!r} must be an object")
                return copy.deepcopy(response)

        available = ", ".join(sorted(self._responses)) or "(none loaded)"
        raise MissingFixtureError(
            "No deterministic E2E LLM fixture for "
            f"method={method!r}, agent={agent_name!r}, "
            f"tool={tool_name!r}, model={model!r}. Available: {available}"
        )

    def _record_call(
        self,
        *,
        method: str,
        agent_name: str,
        tool_name: str | None,
        model: str,
    ) -> None:
        with self._lock:
            self._call_number += 1
            entry = {
                "call": self._call_number,
                "method": method,
                "agent_name": agent_name,
                "tool_name": tool_name,
                "model": model,
            }
            if self._ledger_path is not None:
                self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with self._ledger_path.open("a", encoding="utf-8") as ledger:
                    ledger.write(json.dumps(entry, sort_keys=True) + "\n")

    @staticmethod
    def _usage(response: dict[str, Any], *, stop_reason: str) -> dict[str, Any]:
        usage = dict(_DEFAULT_USAGE)
        usage["stop_reason"] = stop_reason
        raw = response.get("usage") or {}
        if not isinstance(raw, dict):
            raise RuntimeError("E2E LLM fixture usage must be an object")
        usage.update(raw)
        return usage

    @staticmethod
    def _persist_run(
        kwargs: dict[str, Any],
        *,
        started_at: datetime,
        usage: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Mirror the production provider wrappers' AgentRun audit row.

        The E2E installer replaces those wrappers at their dispatch boundary,
        so the fixture client must preserve this observable production
        behavior.  In particular, a clean reviewer pass has no finding rows;
        its completed AgentRun is the truthful submission-readiness marker.
        """
        import app.services.llm as llm_module
        from app.core.enums import AgentRunStatus

        usage = usage or {}
        raw_proposal_id = kwargs.get("proposal_id")
        proposal_id = (
            int(raw_proposal_id) if raw_proposal_id is not None else None
        )
        llm_module._record_run(
            proposal_id=proposal_id,
            agent_name=str(kwargs.get("agent_name") or ""),
            model=str(kwargs.get("model") or ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=float(usage.get("cost_usd") or 0.0),
            started_at=started_at,
            completed_at=datetime.now(UTC),
            status=(
                AgentRunStatus.FAILED
                if error is not None
                else AgentRunStatus.COMPLETED
            ),
            error_text=str(error) if error is not None else None,
        )

    def complete(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        started_at = datetime.now(UTC)
        try:
            agent_name = str(kwargs.get("agent_name") or "")
            model = str(kwargs.get("model") or "")
            response = self._lookup(
                "complete", agent_name=agent_name, model=model,
            )
            if "text" not in response:
                raise RuntimeError(
                    f"E2E complete fixture for {agent_name!r} is missing 'text'"
                )
            usage = self._usage(response, stop_reason="end_turn")
        except Exception as exc:
            self._persist_run(kwargs, started_at=started_at, error=exc)
            raise
        self._persist_run(kwargs, started_at=started_at, usage=usage)
        return str(response["text"]), usage

    def call_tool(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        started_at = datetime.now(UTC)
        try:
            agent_name = str(kwargs.get("agent_name") or "")
            model = str(kwargs.get("model") or "")
            tool = kwargs.get("tool") or {}
            tool_name = (
                str(tool.get("name") or "") if isinstance(tool, dict) else ""
            )
            response = self._lookup(
                "call_tool",
                agent_name=agent_name,
                tool_name=tool_name,
                model=model,
            )
            tool_input = response.get("tool_input")
            if not isinstance(tool_input, dict):
                raise RuntimeError(
                    f"E2E call_tool fixture for {agent_name!r}/{tool_name!r} "
                    "must contain an object-valued 'tool_input'"
                )
            usage = self._usage(response, stop_reason="tool_use")
        except Exception as exc:
            self._persist_run(kwargs, started_at=started_at, error=exc)
            raise
        self._persist_run(kwargs, started_at=started_at, usage=usage)
        return tool_input, usage

    def complete_with_search(
        self, **kwargs: Any,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        return self._search_response("complete_with_search", kwargs)

    def complete_with_web_search(
        self, **kwargs: Any,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        return self._search_response("complete_with_web_search", kwargs)

    def _search_response(
        self, method: str, kwargs: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        started_at = datetime.now(UTC)
        try:
            agent_name = str(kwargs.get("agent_name") or "")
            model = str(kwargs.get("model") or "")
            response = self._lookup(method, agent_name=agent_name, model=model)
            citations = response.get("citations") or []
            if not isinstance(citations, list):
                raise RuntimeError(
                    f"E2E search fixture for {agent_name!r} has non-list citations"
                )
            usage = self._usage(response, stop_reason="end_turn")
        except Exception as exc:
            self._persist_run(kwargs, started_at=started_at, error=exc)
            raise
        self._persist_run(kwargs, started_at=started_at, usage=usage)
        return str(response.get("text") or ""), citations, usage


def install_fixture_llm() -> FixtureLLM:
    """Install one fake client at every provider-dispatch boundary.

    This function deliberately refuses to run unless both E2E guards are set.
    It is called only by ``tests/e2e/support/run_app.py``.
    """
    if os.environ.get("APP_ENV", "").strip().lower() != "e2e":
        raise RuntimeError("Fixture LLM installation requires APP_ENV=e2e")
    if os.environ.get("RFP_E2E_FAKE_LLM", "") != "1":
        raise RuntimeError("Fixture LLM installation requires RFP_E2E_FAKE_LLM=1")

    import app.services.llm as llm_module

    client = FixtureLLM.from_environment()

    def _get_client() -> FixtureLLM:
        return client

    def _call_tool_for_model(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return client.call_tool(**kwargs)

    llm_module.get_anthropic = _get_client  # type: ignore[assignment]
    llm_module.get_gemini = _get_client  # type: ignore[assignment]
    llm_module.get_openai = _get_client  # type: ignore[assignment]
    llm_module.call_tool_for_model = _call_tool_for_model  # type: ignore[assignment]
    llm_module._client_singleton = client  # type: ignore[assignment]
    llm_module._gemini_singleton = client  # type: ignore[assignment]
    llm_module._openai_singleton = client  # type: ignore[assignment]
    return client


__all__ = ["FixtureLLM", "MissingFixtureError", "install_fixture_llm"]
