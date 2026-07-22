from __future__ import annotations

from types import SimpleNamespace

import pytest


def _factory(**kwargs):
    return SimpleNamespace(**kwargs)


def _fake_gemini(response):
    from app.services.llm import GeminiSync

    client = GeminiSync.__new__(GeminiSync)
    client._types = SimpleNamespace(
        Content=_factory,
        Part=SimpleNamespace(from_text=lambda **kwargs: _factory(**kwargs)),
        Tool=_factory,
        FunctionDeclaration=_factory,
        SafetySetting=_factory,
        ToolConfig=_factory,
        FunctionCallingConfig=_factory,
        GenerateContentConfig=_factory,
    )
    client._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_kwargs: response),
    )
    return client


def _call(client):
    return client.call_tool(
        model="gemini-2.5-pro",
        system="system",
        messages=[{"role": "user", "content": "review"}],
        tool={
            "name": "submit_review",
            "description": "Return review findings",
            "input_schema": {"type": "object", "properties": {}},
        },
        agent_name="reviewer_b",
        proposal_id=123,
    )


def test_missing_forced_function_call_is_failed_not_false_clean(monkeypatch) -> None:
    import app.services.llm as llm
    from app.core.enums import AgentRunStatus

    response = SimpleNamespace(
        candidates=[],
        usage_metadata=SimpleNamespace(
            prompt_token_count=50,
            candidates_token_count=0,
        ),
    )
    client = _fake_gemini(response)
    recorded: list[dict] = []
    monkeypatch.setattr(llm, "_record_run", lambda **kwargs: recorded.append(kwargs))

    with pytest.raises(RuntimeError, match="no required function_call"):
        _call(client)

    assert len(recorded) == 1
    assert recorded[0]["status"] == AgentRunStatus.FAILED
    assert "submit_review" in recorded[0]["error_text"]


def test_present_function_call_with_empty_object_is_legitimate(monkeypatch) -> None:
    import app.services.llm as llm
    from app.core.enums import AgentRunStatus

    function_call = SimpleNamespace(name="submit_review", args={})
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[SimpleNamespace(function_call=function_call)],
                ),
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=50,
            candidates_token_count=3,
        ),
    )
    client = _fake_gemini(response)
    recorded: list[dict] = []
    monkeypatch.setattr(llm, "_record_run", lambda **kwargs: recorded.append(kwargs))

    tool_input, usage = _call(client)

    assert tool_input == {}
    assert usage["output_tokens"] == 3
    assert recorded[0]["status"] == AgentRunStatus.COMPLETED


def test_nested_additional_properties_is_removed_only_from_gemini_copy(
    monkeypatch,
) -> None:
    import app.services.llm as llm
    from app.agents.section_m_extractor import _TOOL_SPEC

    function_call = SimpleNamespace(
        name="report_evaluation_criteria",
        args={"evaluation_method": "unknown", "factors": []},
    )
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[SimpleNamespace(function_call=function_call)],
                ),
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=50,
            candidates_token_count=3,
        ),
    )
    client = _fake_gemini(response)
    declarations: list[dict] = []

    def capture_declaration(**kwargs):
        declarations.append(kwargs)
        return _factory(**kwargs)

    client._types.FunctionDeclaration = capture_declaration
    monkeypatch.setattr(llm, "_record_run", lambda **_kwargs: None)

    client.call_tool(
        model="gemini-2.5-pro",
        system="system",
        messages=[{"role": "user", "content": "extract"}],
        tool=_TOOL_SPEC,
        agent_name="section_m_extractor",
    )

    original_map_schema = _TOOL_SPEC["input_schema"]["properties"][
        "section_l_to_m_map"
    ]
    gemini_schema = declarations[0]["parameters"]
    gemini_map_schema = gemini_schema["properties"]["section_l_to_m_map"]

    # The exact nested schema that failed against Generative Language is
    # removed from Gemini's copy, while sibling constraints survive.
    assert "additionalProperties" not in gemini_map_schema
    assert gemini_schema["properties"]["factors"]["items"]["type"] == "object"
    assert gemini_schema["properties"]["factors"]["items"]["properties"][
        "weight_pct"
    ]["type"] == "number"
    assert gemini_schema["properties"]["factors"]["items"]["properties"][
        "weight_pct"
    ]["nullable"] is True

    # Adapting for Gemini must not mutate the shared tool spec used by the
    # Anthropic and OpenAI provider paths.
    assert original_map_schema["additionalProperties"] == {
        "type": "array",
        "items": {"type": "string"},
    }
