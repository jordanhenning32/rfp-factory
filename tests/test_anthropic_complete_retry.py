from __future__ import annotations

from types import SimpleNamespace


class RateLimitError(Exception):
    pass


class APIStatusError(Exception):
    def __init__(self, message: str, *, status_code: int, body: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_anthropic_complete_retries_transient_provider_failure(monkeypatch) -> None:
    import app.services.llm as llm

    calls = {"count": 0}

    def create(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RateLimitError("429 rate limit")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=7, output_tokens=2),
        )

    client = llm.AnthropicSync.__new__(llm.AnthropicSync)
    client._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    recorded: list[dict] = []
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(llm, "_record_run", lambda **kwargs: recorded.append(kwargs))

    text, usage = client.complete(
        model="claude-haiku-4-5-20251001",
        system="system",
        messages=[{"role": "user", "content": "ping"}],
        agent_name="retry_contract",
    )

    assert text == "ok"
    assert usage["output_tokens"] == 2
    assert calls["count"] == 2
    assert len(recorded) == 1
    assert recorded[0]["status"] == llm.AgentRunStatus.COMPLETED


def test_anthropic_complete_does_not_retry_non_transient_failure(monkeypatch) -> None:
    import app.services.llm as llm

    calls = {"count": 0}

    def create(**_kwargs):
        calls["count"] += 1
        raise ValueError("invalid request")

    client = llm.AnthropicSync.__new__(llm.AnthropicSync)
    client._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    recorded: list[dict] = []
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(llm, "_record_run", lambda **kwargs: recorded.append(kwargs))

    try:
        client.complete(
            model="claude-haiku-4-5-20251001",
            system="system",
            messages=[{"role": "user", "content": "ping"}],
            agent_name="retry_contract",
        )
    except ValueError as exc:
        assert "invalid request" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")

    assert calls["count"] == 1
    assert len(recorded) == 1
    assert recorded[0]["status"] == llm.AgentRunStatus.FAILED


def test_anthropic_complete_retries_explicit_overload(monkeypatch) -> None:
    import app.services.llm as llm

    calls = {"count": 0}

    def create(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise APIStatusError(
                "Overloaded",
                status_code=529,
                body={
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "Overloaded",
                    },
                },
            )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=7, output_tokens=2),
        )

    client = llm.AnthropicSync.__new__(llm.AnthropicSync)
    client._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    recorded: list[dict] = []
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(llm, "_record_run", lambda **kwargs: recorded.append(kwargs))

    text, _usage = client.complete(
        model="claude-haiku-4-5-20251001",
        system="system",
        messages=[{"role": "user", "content": "ping"}],
        agent_name="retry_contract",
    )

    assert text == "ok"
    assert calls["count"] == 2
    assert len(recorded) == 1
    assert recorded[0]["status"] == llm.AgentRunStatus.COMPLETED


def test_overload_word_alone_is_not_a_generic_retry_signal() -> None:
    import app.services.llm as llm

    assert not llm._is_transient_error(ValueError("worker overloaded locally"))


def test_protocol_identifiers_are_not_mistaken_for_http_statuses() -> None:
    import app.services.llm as llm

    assert not llm._is_transient_error(
        ValueError("unknown requirement_id REQ-502")
    )
    assert not llm._is_transient_error(
        ValueError("candidate source_page 503 is outside page 1")
    )


def test_contextual_or_typed_transient_http_statuses_are_detected() -> None:
    import app.services.llm as llm

    class ProviderError(Exception):
        status_code = 504

    for message in (
        "status code: 429 rate limit",
        "HTTP 502",
        "response=503 service unavailable",
        "error code 504 gateway timeout",
    ):
        assert llm._is_transient_error(RuntimeError(message))
    assert llm._is_transient_error(ProviderError("request failed"))
