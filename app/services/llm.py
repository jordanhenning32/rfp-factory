"""LLM client wrappers + cost tracking.

Every call records token usage and approximate USD cost to the agent_runs
table so the cost dashboard (Phase 1 hardening) has data from day one.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from anthropic import Anthropic
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.enums import AgentRunStatus
from app.db.session import session_scope
from app.models import AgentRun, Proposal

log = logging.getLogger(__name__)


# ---- Transient-error retry helper ---------------------------------------
# Used by all three provider call_tool methods. Parallelized auto-loops
# fan out N concurrent requests against the same model and routinely trip
# both rate limits (429) and transient server errors (502/503/504) during
# peak periods; transparent retry keeps the loop healthy without the
# caller having to think about it. 500 Internal Server Error is NOT
# retried — those can be deterministic code-side bugs rather than transient.

_T = TypeVar("_T")
_TRANSIENT_BACKOFF = (2.0, 4.0, 8.0)  # delays in seconds, max 3 retries


def _is_anthropic_overload_error(exc: BaseException) -> bool:
    """Return whether *exc* carries Anthropic's explicit overload signal.

    Anthropic currently surfaces overloads as ``APIStatusError`` with HTTP
    529 and an ``overloaded_error`` body.  Some SDK/transport paths retain
    only the short ``Overloaded`` message, so accept that exact message for
    the same exception class as a narrow fallback.  Deliberately avoid a
    broad ``"overload" in message`` check: application errors can contain
    that word without being safe to retry.
    """
    if type(exc).__name__ != "APIStatusError":
        return False

    if getattr(exc, "status_code", None) == 529:
        return True

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("type") == "overloaded_error":
            return True

    return str(exc).strip().lower() == "overloaded"


def _is_transient_error(exc: BaseException) -> bool:
    """Detect a transient / retryable error across providers.

    Covers two classes of failure:
      1. Rate limits / quota exhaustion (429 family).
      2. Transient server unavailability (502 / 503 / 504), including
         Anthropic's explicit 529 ``overloaded_error`` response.

    We do this by class name + error-message inspection rather than
    importing each SDK's typed exception, so the helper still works if
    a provider SDK is missing or its types change between versions.
    """
    cls_name = type(exc).__name__
    # Rate-limit / quota class names across providers.
    if cls_name in ("RateLimitError", "ResourceExhausted"):
        return True
    # Server-unavailable class names: google.api_core's ServiceUnavailable
    # and InternalServerError-style exceptions; anthropic / openai also
    # surface APIStatusError which carries the status code in the message.
    if cls_name in ("ServiceUnavailable", "BadGateway", "GatewayTimeout"):
        return True
    if _is_anthropic_overload_error(exc):
        return True
    status_code = getattr(exc, "status_code", None)
    if (
        not isinstance(status_code, bool)
        and isinstance(status_code, int)
        and status_code in {429, 502, 503, 504}
    ):
        return True
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if (
        not isinstance(response_status, bool)
        and isinstance(response_status, int)
        and response_status in {429, 502, 503, 504}
    ):
        return True
    msg = str(exc).lower()
    contextual_http_status = re.search(
        r"\b(?:http(?:\s+status)?|status(?:\s+code)?|error(?:\s+code)?|response)"
        r"\s*[:=]?\s*(?:429|502|503|504)\b",
        msg,
    ) is not None
    return (
        # Rate-limit signals
        contextual_http_status
        or "rate limit" in msg
        or "rate-limit" in msg
        or "rate_limit" in msg
        or "quota" in msg
        or "too many requests" in msg
        # Transient server signals
        or "service unavailable" in msg
        or "bad gateway" in msg
        or "gateway timeout" in msg
    )


def is_transient_provider_error(exc: BaseException) -> bool:
    """Public shared classifier for callers deciding retry/fallback strategy."""

    return _is_transient_error(exc)


def _retry_on_transient_error(
    fn: Callable[[], _T], *, agent_name: str, model: str
) -> _T:
    """Call `fn()`; if it raises a transient error (rate limit OR
    502/503/504/provider overload), sleep and retry up to
    len(_TRANSIENT_BACKOFF) times
    with exponential backoff. Any non-transient error is re-raised
    immediately so the caller's normal error handling (failure
    recording, temperature-fallback, etc.) runs.
    """
    for attempt in range(len(_TRANSIENT_BACKOFF) + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient_error(exc):
                raise
            if attempt >= len(_TRANSIENT_BACKOFF):
                log.warning(
                    "%s on %s: transient-error retries exhausted (%d attempts) — re-raising.",
                    agent_name,
                    model,
                    attempt + 1,
                )
                raise
            delay = _TRANSIENT_BACKOFF[attempt]
            log.warning(
                "%s on %s: transient error (attempt %d/%d) — sleeping %.1fs then retrying. err=%s",
                agent_name,
                model,
                attempt + 1,
                len(_TRANSIENT_BACKOFF) + 1,
                delay,
                str(exc)[:160],
            )
            time.sleep(delay)
    # unreachable: the loop either returns or re-raises
    raise RuntimeError("unreachable")


# ---- Usage logging helper ------------------------------------------------
# Every agent log line ends with the same "in=N out=N cost=$X.XXXX" pattern
# (some also include cache_read=N when cache hits exist). Centralizing the
# formatting here means the format stays consistent across all agents and a
# future change (e.g., adding a thinking-tokens field) lands in one place.


def fmt_llm_usage(usage: dict[str, Any]) -> str:
    """Compact 'in=N out=N cost=$X.XXXX' suffix for agent log lines.

    When the usage dict contains a non-zero `cache_read_tokens` field
    (Anthropic prompt caching), it's slotted in between in= and out=.
    Missing keys default to 0 / 0.0 so callers can pass partial usage
    dicts without guarding each lookup.

    Example:
        log.info("intake_metadata: %s", fmt_llm_usage(usage))
        # → "intake_metadata: in=1234 out=87 cost=$0.0123"
    """
    parts = [f"in={usage.get('input_tokens', 0)}"]
    cache_read = usage.get("cache_read_tokens") or 0
    if cache_read:
        parts.append(f"cache_read={cache_read}")
    parts.append(f"out={usage.get('output_tokens', 0)}")
    parts.append(f"cost=${float(usage.get('cost_usd') or 0.0):.4f}")
    return " ".join(parts)


# Approximate USD per million tokens. Update as providers publish new pricing.
# Tuple is (input_per_mtok, output_per_mtok). Unknown models fall back to
# Sonnet pricing as the safe overestimate.
ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    # Haiku — cheap structured extraction
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    # Sonnet — drafting + Reviewer A (override)
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    # Opus — Reviewer A default (heaviest reasoning)
    "claude-opus-4-7": (15.00, 75.00),
}
GEMINI_PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-001": (0.10, 0.40),
    "gemini-2.0-pro": (1.25, 5.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3-pro": (2.00, 15.00),  # estimate — verify when GA
    "gemini-3.5-pro": (2.00, 15.00),  # estimate — verify when GA
    "gemini-3-flash": (0.40, 3.00),  # estimate
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
}
# OpenAI prices — verify against current OpenAI pricing page; these are
# best-known estimates as of early 2026 and may need updating.
OPENAI_PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.5": (2.50, 10.00),
    "gpt-5": (2.50, 10.00),
    "gpt-5-mini": (0.30, 1.50),
    "gpt-5-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3": (5.00, 20.00),
    "o3-mini": (1.10, 4.40),
}
_DEFAULT_PRICE = (3.00, 15.00)


def estimate_anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = ANTHROPIC_PRICES.get(model, _DEFAULT_PRICE)
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def _anthropic_model_supports_temperature(model: str) -> bool:
    """Anthropic deprecated the `temperature` parameter on Opus 4.7+.
    Sending temperature returns a 400 BadRequestError saying
    "temperature is deprecated for this model." Detect those models and
    skip the parameter entirely. Older models (Sonnet 4.6, Haiku 4.5,
    earlier Opus) still accept it.
    """
    if model.startswith("claude-opus-4-7"):
        return False
    # Future-proof for the next-generation Anthropic models that follow the
    # same deprecation pattern.
    if model.startswith(("claude-opus-4-8", "claude-opus-5", "claude-sonnet-5")):
        return False
    return True


def estimate_gemini_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = GEMINI_PRICES.get(model, GEMINI_PRICES["gemini-2.0-flash"])
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def estimate_openai_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """OpenAI auto-caches reusable prompt prefixes. Cached read tokens bill
    at ~50% of the base input rate (per OpenAI's published pricing)."""
    in_price, out_price = OPENAI_PRICES.get(model, _DEFAULT_PRICE)
    base_input = max(input_tokens - cached_input_tokens, 0)
    return (
        base_input * in_price + cached_input_tokens * in_price * 0.5 + output_tokens * out_price
    ) / 1_000_000


def _record_run(
    *,
    proposal_id: int | None,
    agent_name: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    started_at: datetime,
    completed_at: datetime,
    status: AgentRunStatus,
    error_text: str | None = None,
) -> None:
    """Persist a row in agent_runs. Skips if no proposal_id (e.g., pre-creation
    extraction during the upload flow)."""
    if proposal_id is None:
        log.info(
            "agent_run not persisted (no proposal yet) — agent=%s model=%s in=%d out=%d $%.4f",
            agent_name,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
        )
        return
    try:
        with session_scope() as db:
            exists = db.scalar(select(Proposal.id).where(Proposal.id == proposal_id))
            if exists is None:
                log.info(
                    "agent_run not persisted (proposal %d does not exist) -- agent=%s model=%s",
                    proposal_id,
                    agent_name,
                    model,
                )
                return
            db.add(
                AgentRun(
                    proposal_id=proposal_id,
                    agent_name=agent_name,
                    model_used=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    started_at=started_at,
                    completed_at=completed_at,
                    status=status,
                    error_text=error_text,
                )
            )
    except IntegrityError:
        log.warning(
            "agent_run insert raced with a Proposal deletion (proposal_id=%d) — skipping persistence for %s",
            proposal_id,
            agent_name,
        )
    except Exception:
        log.exception("failed to record agent_run for %s", agent_name)


class AnthropicSync:
    """Thin sync wrapper around Anthropic SDK with cost tracking."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in .env — required for agent calls.")
        self._client = Anthropic(api_key=settings.anthropic_api_key)

    def complete(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
        agent_name: str,
        proposal_id: int | None = None,
        temperature: float = 0.0,
    ) -> tuple[str, dict[str, Any]]:
        """Call Anthropic's messages API, return (text, usage_dict).

        usage_dict contains: input_tokens, output_tokens, cost_usd.
        """
        started = datetime.now(UTC)
        try:
            resp = _retry_on_transient_error(
                lambda: self._client.messages.create(
                    model=model,
                    system=system or "",
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
                agent_name=agent_name,
                model=model,
            )
        except Exception as exc:
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                started_at=started,
                completed_at=datetime.now(UTC),
                status=AgentRunStatus.FAILED,
                error_text=str(exc),
            )
            raise

        text = "".join(
            getattr(block, "text", "") for block in resp.content if getattr(block, "type", "") == "text"
        )
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cost = estimate_anthropic_cost(model, in_tok, out_tok)

        _record_run(
            proposal_id=proposal_id,
            agent_name=agent_name,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            started_at=started,
            completed_at=datetime.now(UTC),
            status=AgentRunStatus.COMPLETED,
        )
        return text, {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": cost}

    def call_tool(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        max_tokens: int = 8000,
        agent_name: str,
        proposal_id: int | None = None,
        temperature: float = 0.0,
        cached_prefix: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Force the model to call a single tool and return (tool_input_dict, usage).

        `tool` is the tool spec dict with name, description, input_schema.
        Anthropic guarantees the response will match the input_schema, so the
        returned dict is structurally valid.

        If `cached_prefix` is provided (a large static text block that's
        reused across many calls), it's added to the system as a cached
        block via Anthropic's ephemeral prompt cache (5-min TTL). Subsequent
        calls with the same prefix pay ~10% of base input cost on those
        tokens. Use for: profile + KB context shared across compliance
        batches in the Shortfall Strategist.
        """
        started = datetime.utcnow()
        if cached_prefix:
            system_param: Any = [
                {"type": "text", "text": system or ""},
                {
                    "type": "text",
                    "text": cached_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        else:
            system_param = system or ""

        # Use streaming to avoid the SDK's 10-minute non-streaming limit
        # (max_tokens > ~21K with Sonnet trips ValueError). Streaming has no
        # such cap; we drain the events and fetch the assembled final message.
        # Anthropic deprecated temperature on Opus 4.7+; for those models we
        # omit the parameter (sending it returns 400 BadRequestError).
        stream_kwargs: dict[str, Any] = dict(
            model=model,
            system=system_param,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            max_tokens=max_tokens,
        )
        if _anthropic_model_supports_temperature(model):
            stream_kwargs["temperature"] = temperature

        def _run_stream() -> Any:
            with self._client.messages.stream(**stream_kwargs) as st:
                for _ in st:
                    pass
                return st.get_final_message()

        def _attempt() -> Any:
            """One full attempt: try once, recover from temperature
            deprecation by dropping the parameter and retrying once.
            Rate-limit errors propagate so the outer retry can handle them.
            """
            try:
                return _run_stream()
            except Exception as exc:
                # Don't swallow rate-limit errors here — let them bubble
                # to _retry_on_transient_error.
                if _is_transient_error(exc):
                    raise
                # Belt-and-suspenders: if the model rejected temperature and
                # our pattern detection didn't catch it (future model class
                # outside the prefix list), retry without temperature.
                err_text = str(exc).lower()
                if (
                    "temperature" in stream_kwargs
                    and "temperature" in err_text
                    and (
                        "deprecated" in err_text
                        or "does not support" in err_text
                        or "unsupported" in err_text
                    )
                ):
                    log.warning(
                        "%s: Anthropic model %s rejected temperature=%s; retrying without it.",
                        agent_name,
                        model,
                        temperature,
                    )
                    stream_kwargs.pop("temperature", None)
                    return _run_stream()
                raise

        try:
            resp = _retry_on_transient_error(
                _attempt,
                agent_name=agent_name,
                model=model,
            )
        except Exception as exc:
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                started_at=started,
                completed_at=datetime.utcnow(),
                status=AgentRunStatus.FAILED,
                error_text=str(exc),
            )
            raise

        # Find the tool_use block — there should be exactly one given tool_choice.
        tool_input: dict[str, Any] | None = None
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == tool["name"]:
                tool_input = dict(block.input)  # copy out of SDK type
                break

        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cache_create = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        stop_reason = getattr(resp, "stop_reason", None)
        # Cost formula with prompt cache multipliers (Anthropic published rates):
        # base input × 1.0, cache write × 1.25, cache read × 0.10.
        in_price, out_price = ANTHROPIC_PRICES.get(model, _DEFAULT_PRICE)
        cost = (
            in_tok * in_price
            + cache_create * in_price * 1.25
            + cache_read * in_price * 0.10
            + out_tok * out_price
        ) / 1_000_000

        if tool_input is None:
            err = (
                f"Model did not return a tool_use block "
                f"(stop_reason={stop_reason}, in={in_tok}, out={out_tok})."
            )
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                started_at=started,
                completed_at=datetime.utcnow(),
                status=AgentRunStatus.FAILED,
                error_text=err,
            )
            raise RuntimeError(err)

        # Surface truncation early — tool_use truncated mid-JSON returns
        # empty/partial input that quietly looks "successful".
        if stop_reason == "max_tokens":
            log.warning(
                "%s: stop_reason=max_tokens — output was truncated at %d tokens. "
                "Tool input keys: %s. Bump max_tokens or split the input.",
                agent_name,
                out_tok,
                list(tool_input.keys()),
            )

        # Log cache metrics so we can verify caching is paying off.
        if cache_create or cache_read:
            log.info(
                "%s cache: write=%d read=%d (read = ~10%% of base cost)",
                agent_name,
                cache_create,
                cache_read,
            )

        _record_run(
            proposal_id=proposal_id,
            agent_name=agent_name,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            started_at=started,
            completed_at=datetime.utcnow(),
            status=AgentRunStatus.COMPLETED,
        )
        return tool_input, {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_tokens": cache_create,
            "cache_read_tokens": cache_read,
            "cost_usd": cost,
            "stop_reason": stop_reason,
        }

    def complete_with_web_search(
        self,
        *,
        model: str,
        system: str | None,
        user_prompt: str,
        max_tokens: int = 8000,
        agent_name: str,
        proposal_id: int | None = None,
        temperature: float = 0.0,
        max_uses: int = 5,
    ) -> tuple[str, list[dict], dict[str, Any]]:
        """Free-form Claude call with the `web_search_20250305` tool
        enabled. Returns (text, citations, usage). Mirrors the shape of
        the Gemini grounded equivalent (`complete_with_search`) so the
        two can be swapped behind a dual-pipeline orchestrator.

        `citations` is a deduped list of `{title, uri}` dicts pulled
        from the text block's inline citation annotations and from any
        `web_search_tool_result` blocks the model produced.

        `usage["cost_usd"]` includes both the LLM token cost and the
        per-search add-on charge ($10/1k searches at the time of
        writing) so callers can budget honestly.

        Streaming is required because tool-use over web search routinely
        exceeds the SDK's non-streaming max-tokens cap on long answers.
        """
        started = datetime.utcnow()
        tools = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_uses,
            }
        ]

        def _run_stream():
            with self._client.messages.stream(
                model=model,
                system=system or "",
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=max_tokens,
                tools=tools,
                temperature=temperature,
            ) as st:
                for _ in st:
                    pass
                return st.get_final_message()

        try:
            resp = _retry_on_transient_error(
                _run_stream,
                agent_name=agent_name,
                model=model,
            )
        except Exception as exc:
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                started_at=started,
                completed_at=datetime.utcnow(),
                status=AgentRunStatus.FAILED,
                error_text=str(exc),
            )
            raise

        text_parts: list[str] = []
        citations: list[dict] = []
        seen_urls: set[str] = set()

        def _add_citation(title: str, url: str) -> None:
            url = (url or "").strip()
            title = (title or "").strip()
            if not url and not title:
                return
            key = url or title
            if key in seen_urls:
                return
            seen_urls.add(key)
            citations.append({"title": title, "uri": url})

        for block in resp.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
                for cite in getattr(block, "citations", None) or []:
                    _add_citation(
                        getattr(cite, "title", "") or "",
                        getattr(cite, "url", "") or "",
                    )
            elif block_type == "web_search_tool_result":
                for result in getattr(block, "content", None) or []:
                    _add_citation(
                        getattr(result, "title", "") or "",
                        getattr(result, "url", "") or "",
                    )

        text = "".join(text_parts).strip()
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cost = estimate_anthropic_cost(model, in_tok, out_tok)

        # Add the per-search add-on. Anthropic charges $10/1k web
        # searches; usage.server_tool_use.web_search_requests is the
        # canonical count when present.
        server_tool_use = getattr(resp.usage, "server_tool_use", None)
        n_web_searches = (
            getattr(server_tool_use, "web_search_requests", 0) or 0 if server_tool_use is not None else 0
        )
        cost += 0.01 * n_web_searches

        _record_run(
            proposal_id=proposal_id,
            agent_name=agent_name,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            started_at=started,
            completed_at=datetime.utcnow(),
            status=AgentRunStatus.COMPLETED,
        )
        log.info(
            "%s (web_search): %d citations, %d searches, in=%d out=%d cost=$%.4f",
            agent_name,
            len(citations),
            n_web_searches,
            in_tok,
            out_tok,
            cost,
        )
        return (
            text,
            citations,
            {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": cost,
                "web_searches": n_web_searches,
            },
        )


# Lazy singleton — first agent call constructs it, subsequent calls reuse.
_client_singleton: AnthropicSync | None = None


def get_anthropic() -> AnthropicSync:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = AnthropicSync()
    return _client_singleton


# ---- Gemini (Reviewer B + Cost Reviewer) -----------------------------------

def _adapt_json_schema_for_gemini(value: Any) -> Any:
    """Copy a JSON Schema while removing Gemini-unsupported keywords.

    The google-genai SDK's ``Schema`` model accepts ``additionalProperties``,
    but the Generative Language backend currently rejects the serialized
    ``additional_properties`` field.  Tool schemas can contain that keyword
    at any depth (Section M's requirement-to-factor map is one example), so
    adapt recursively at the Gemini boundary.  The SDK's OpenAPI ``Schema``
    type also represents a nullable value as ``type`` + ``nullable`` rather
    than JSON Schema's ``type: [T, "null"]``; normalize that equivalent form
    while walking the tree.  Copy-on-write is important: Anthropic and OpenAI
    continue to receive the original, stronger schema.
    """
    if isinstance(value, dict):
        adapted = {
            key: _adapt_json_schema_for_gemini(child)
            for key, child in value.items()
            if key != "additionalProperties"
        }
        schema_types = adapted.get("type")
        if (
            isinstance(schema_types, list)
            and len(schema_types) == 2
            and "null" in schema_types
        ):
            non_null_types = [item for item in schema_types if item != "null"]
            if len(non_null_types) == 1:
                adapted["type"] = non_null_types[0]
                adapted["nullable"] = True
        return adapted
    if isinstance(value, list):
        return [_adapt_json_schema_for_gemini(child) for child in value]
    return value

class GeminiSync:
    """Thin sync wrapper around Google's google-genai SDK with a `call_tool`
    interface that mirrors AnthropicSync.call_tool — same return shape, so
    agent code can swap providers without conditionals at the call site.

    Forced-tool semantics: we always call with mode='ANY' + an allow-list of
    one tool, so the response is guaranteed to contain exactly one
    function_call with structured args matching the JSON schema we provide.
    """

    def __init__(self) -> None:
        settings = get_settings()
        api_key = settings.google_or_gemini_key
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) not set in .env — required for Gemini agent calls."
            )
        try:
            from google import genai  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "google-genai not installed. Install with `pip install -e .[llm_extra]` "
                "(it's in the optional llm_extra dependencies group)."
            ) from exc
        self._client = genai.Client(api_key=api_key)
        self._genai = genai
        try:
            from google.genai import types  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("google-genai types module not available.") from exc
        self._types = types

    def call_tool(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        max_tokens: int = 8000,
        agent_name: str,
        proposal_id: int | None = None,
        temperature: float = 0.0,
        cached_prefix: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Call Gemini's generate_content with a forced function call.

        `cached_prefix` is concatenated into the system instruction (Gemini
        doesn't have Anthropic-style ephemeral cache — it has its own
        explicit cached-content API but it's heavier weight, not worth
        the complexity here).

        Returns (tool_input_dict, usage_dict) with the same shape as
        AnthropicSync.call_tool.
        """
        started = datetime.now(UTC)

        # Build the system instruction (system + optional cached prefix).
        sys_text = system or ""
        if cached_prefix:
            sys_text = f"{sys_text}\n\n{cached_prefix}" if sys_text else cached_prefix

        # Convert {role, content} messages into google-genai contents.
        # We only use 'user' messages here; 'assistant' would be modeled but
        # we don't multi-turn the reviewer.
        contents: list = []
        for m in messages:
            role = m.get("role", "user")
            text = m.get("content", "")
            if role == "user":
                contents.append(
                    self._types.Content(
                        role="user",
                        parts=[self._types.Part.from_text(text=text)],
                    )
                )
            else:
                # Future-proof: model role for assistant turns.
                contents.append(
                    self._types.Content(
                        role="model",
                        parts=[self._types.Part.from_text(text=text)],
                    )
                )

        # Build the tool spec. Gemini's function_declarations expects the
        # JSON schema in the `parameters` field — same shape as Anthropic's
        # input_schema.
        tool_decl = self._types.Tool(function_declarations=[
            self._types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=_adapt_json_schema_for_gemini(tool["input_schema"]),
            )
        ])

        # Disable Gemini's content moderation. Proposal text is policy /
        # corporate / technical content — there's no scenario where Gemini's
        # safety filters should be screening it. Without BLOCK_NONE, Gemini
        # occasionally returns 200 OK with 0 output tokens because some
        # phrase ("data security", "weapons", "compromise") tripped a
        # filter and silently stripped the response.
        try:
            safety_settings = [
                self._types.SafetySetting(
                    category=cat,
                    threshold="BLOCK_NONE",
                )
                for cat in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_CIVIC_INTEGRITY",
                )
            ]
        except Exception:
            # If the SDK rejects a category name (newer/older SDK), fall back
            # to no safety_settings — the call still works, filters just stay
            # at default. We log instead of crash.
            log.warning(
                "%s: could not build Gemini safety_settings; proceeding without them.",
                agent_name,
            )
            safety_settings = None

        config_kwargs: dict[str, Any] = dict(
            system_instruction=sys_text or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            tools=[tool_decl],
            tool_config=self._types.ToolConfig(
                function_calling_config=self._types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=[tool["name"]],
                ),
            ),
        )
        if safety_settings is not None:
            config_kwargs["safety_settings"] = safety_settings
        config = self._types.GenerateContentConfig(**config_kwargs)

        try:
            resp = _retry_on_transient_error(
                lambda: self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                ),
                agent_name=agent_name,
                model=model,
            )
        except Exception as exc:
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                started_at=started,
                completed_at=datetime.now(UTC),
                status=AgentRunStatus.FAILED,
                error_text=str(exc),
            )
            raise

        # Pull the function call out. With mode='ANY' + allowed_function_names,
        # Gemini emits a single function_call part on the first candidate.
        tool_input: dict[str, Any] | None = None
        candidates = getattr(resp, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", "") == tool["name"]:
                    args = getattr(fc, "args", None)
                    # args may be a Mapping-like or dict-like; coerce to dict.
                    if args is None:
                        tool_input = {}
                    elif hasattr(args, "items"):
                        tool_input = dict(args)
                    else:
                        # Last-ditch: try JSON round-trip
                        try:
                            tool_input = json.loads(json.dumps(args, default=str))
                        except Exception:
                            tool_input = {}
                    break
            if tool_input is not None:
                break

        usage_meta = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage_meta, "prompt_token_count", 0) or 0
        out_tok = getattr(usage_meta, "candidates_token_count", 0) or 0
        cost = estimate_gemini_cost(model, in_tok, out_tok)

        if tool_input is None:
            # A forced-tool request without a function call is not a clean,
            # empty domain result. It means the provider stripped or failed
            # the structured response. Treating it as ``{}`` can turn a
            # broken reviewer/extractor into a false "zero findings" success.
            error = RuntimeError(
                f"Gemini returned no required function_call {tool['name']!r} "
                f"(model={model}, input_tokens={in_tok}, "
                f"output_tokens={out_tok}, candidates={len(candidates)})"
            )
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                started_at=started,
                completed_at=datetime.now(UTC),
                status=AgentRunStatus.FAILED,
                error_text=str(error),
            )
            raise error

        _record_run(
            proposal_id=proposal_id,
            agent_name=agent_name,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            started_at=started,
            completed_at=datetime.now(UTC),
            status=AgentRunStatus.COMPLETED,
        )
        return tool_input, {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": cost,
            "stop_reason": None,
        }

    def complete_with_search(
        self,
        *,
        model: str,
        system: str | None,
        user_prompt: str,
        max_tokens: int = 6000,
        agent_name: str,
        proposal_id: int | None = None,
        temperature: float = 0.0,
    ) -> tuple[str, list[dict], dict[str, Any]]:
        """Free-form Gemini call with Google Search grounding enabled.

        Returns (text, citations, usage). The model performs live web
        searches as needed and answers in plain text; we don't try to
        force a structured function call here because Gemini disallows
        combining `tools=[GoogleSearch]` with forced-tool semantics.
        Callers structure the output with a follow-up call to a cheap
        model (Haiku) — see app/agents/teaming_researcher.py for the
        two-step pattern.

        `citations` is a list of {title, uri} dicts pulled from the
        response's grounding_metadata, when present, so callers can
        surface "sourced from these URLs" to the user if they want.
        """
        started = datetime.utcnow()
        sys_text = system or ""

        contents = [
            self._types.Content(
                role="user",
                parts=[self._types.Part.from_text(text=user_prompt)],
            ),
        ]

        # Disable Gemini's content moderation; same rationale as call_tool.
        try:
            safety_settings = [
                self._types.SafetySetting(
                    category=cat,
                    threshold="BLOCK_NONE",
                )
                for cat in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_CIVIC_INTEGRITY",
                )
            ]
        except Exception:
            log.warning(
                "%s: could not build Gemini safety_settings; proceeding without them.",
                agent_name,
            )
            safety_settings = None

        config_kwargs: dict[str, Any] = dict(
            system_instruction=sys_text or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            tools=[self._types.Tool(google_search=self._types.GoogleSearch())],
        )
        if safety_settings is not None:
            config_kwargs["safety_settings"] = safety_settings
        config = self._types.GenerateContentConfig(**config_kwargs)

        try:
            resp = _retry_on_transient_error(
                lambda: self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                ),
                agent_name=agent_name,
                model=model,
            )
        except Exception as exc:
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                started_at=started,
                completed_at=datetime.utcnow(),
                status=AgentRunStatus.FAILED,
                error_text=str(exc),
            )
            raise

        # Extract text + citations from the first candidate.
        text_parts: list[str] = []
        citations: list[dict] = []
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            cand = candidates[0]
            content = getattr(cand, "content", None)
            if content is not None:
                for part in getattr(content, "parts", None) or []:
                    txt = getattr(part, "text", "") or ""
                    if txt:
                        text_parts.append(txt)
            # Pull grounding citations if any.
            grounding = getattr(cand, "grounding_metadata", None)
            chunks = getattr(grounding, "grounding_chunks", None) or [] if grounding is not None else []
            for ch in chunks:
                web = getattr(ch, "web", None)
                if web is None:
                    continue
                title = getattr(web, "title", "") or ""
                uri = getattr(web, "uri", "") or ""
                if title or uri:
                    citations.append({"title": title, "uri": uri})

        text = "".join(text_parts).strip()

        usage_meta = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage_meta, "prompt_token_count", 0) or 0
        out_tok = getattr(usage_meta, "candidates_token_count", 0) or 0
        cost = estimate_gemini_cost(model, in_tok, out_tok)

        _record_run(
            proposal_id=proposal_id,
            agent_name=agent_name,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            started_at=started,
            completed_at=datetime.utcnow(),
            status=AgentRunStatus.COMPLETED,
        )
        log.info(
            "%s (grounded): %d citations, in=%d out=%d cost=$%.4f",
            agent_name,
            len(citations),
            in_tok,
            out_tok,
            cost,
        )
        return (
            text,
            citations,
            {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": cost,
            },
        )


_gemini_singleton: GeminiSync | None = None


def get_gemini() -> GeminiSync:
    global _gemini_singleton
    if _gemini_singleton is None:
        _gemini_singleton = GeminiSync()
    return _gemini_singleton


# ---- OpenAI (Reviewer A by default; available for any agent) -------------


class OpenAISync:
    """Thin sync wrapper around OpenAI's SDK with a `call_tool` interface
    that mirrors AnthropicSync.call_tool — same return shape so agents can
    swap providers via config without conditionals at the call site.

    Forced function-call semantics: tool_choice pins the model to a single
    named function; the response is guaranteed to contain a tool_call with
    JSON args matching the schema.

    Prompt caching: OpenAI auto-caches prefixes ≥1024 tokens that are
    reused within ~5-10 minutes. We just include the cached_prefix in the
    system message and let OpenAI handle it; the response's
    `prompt_tokens_details.cached_tokens` reports how many tokens hit cache.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY not set in .env — required for OpenAI agent calls.")
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "openai SDK not installed. Install with "
                "`pip install -e .[llm_extra]` (it's in the optional "
                "llm_extra dependencies group)."
            ) from exc
        # max_retries=2 (default is higher and can take many minutes on
        # transient 5xx); timeout=180 caps any single call so the auto loop
        # can't hang for 10+ minutes blocked on the OpenAI SDK's retry chain.
        # Cancel checkpoints can't fire while a call is in flight, so a
        # bounded call duration is what makes Cancel responsive.
        self._client = OpenAI(
            api_key=settings.openai_api_key,
            max_retries=2,
            timeout=180.0,
        )

    def call_tool(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        max_tokens: int = 8000,
        agent_name: str,
        proposal_id: int | None = None,
        temperature: float = 0.0,
        cached_prefix: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        started = datetime.utcnow()

        # OpenAI doesn't have explicit cache_control like Anthropic — it
        # auto-caches reusable prefixes. Concatenate system + cached_prefix
        # so the prefix appears at a stable offset across calls.
        sys_text = system or ""
        if cached_prefix:
            sys_text = f"{sys_text}\n\n{cached_prefix}" if sys_text else cached_prefix

        api_messages: list[dict[str, Any]] = []
        if sys_text:
            api_messages.append({"role": "system", "content": sys_text})
        for m in messages:
            api_messages.append(
                {
                    "role": m.get("role", "user"),
                    "content": m.get("content", ""),
                }
            )

        # Translate our internal tool spec to OpenAI's function-call format.
        # The `parameters` field expects the JSON schema directly, same as
        # our `input_schema` shape.
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                },
            }
        ]
        oai_tool_choice = {
            "type": "function",
            "function": {"name": tool["name"]},
        }

        # Build kwargs once so we can conditionally include temperature.
        # Reasoning-class models (gpt-5+, o-series) reject any temperature
        # other than the default; for those we omit the parameter.
        create_kwargs: dict[str, Any] = dict(
            model=model,
            messages=api_messages,
            tools=oai_tools,
            tool_choice=oai_tool_choice,
            max_completion_tokens=max_tokens,
        )
        if _openai_model_supports_temperature(model):
            create_kwargs["temperature"] = temperature

        def _attempt() -> Any:
            """One full attempt: try once; recover from temperature-rejection
            by dropping the parameter and retrying once. Rate-limit errors
            propagate so the outer retry can handle them.
            """
            try:
                return self._client.chat.completions.create(**create_kwargs)
            except Exception as exc:
                if _is_transient_error(exc):
                    raise
                # Belt-and-suspenders: if temperature was sent and the model
                # rejected it (an unknown model class outside our pattern),
                # retry without temperature so a misclassified model still
                # works.
                err_text = str(exc).lower()
                if (
                    "temperature" in create_kwargs
                    and "temperature" in err_text
                    and ("does not support" in err_text or "unsupported" in err_text)
                ):
                    log.warning(
                        "%s: model %s rejected temperature=%s; retrying without it.",
                        agent_name,
                        model,
                        temperature,
                    )
                    create_kwargs.pop("temperature", None)
                    return self._client.chat.completions.create(**create_kwargs)
                raise

        try:
            resp = _retry_on_transient_error(
                _attempt,
                agent_name=agent_name,
                model=model,
            )
        except Exception as exc:
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                started_at=started,
                completed_at=datetime.utcnow(),
                status=AgentRunStatus.FAILED,
                error_text=str(exc),
            )
            raise

        choice = resp.choices[0] if resp.choices else None
        msg = choice.message if choice else None
        finish_reason = getattr(choice, "finish_reason", None) if choice else None

        tool_input: dict[str, Any] | None = None
        if msg and getattr(msg, "tool_calls", None):
            tc = msg.tool_calls[0]
            try:
                tool_input = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                log.exception(
                    "%s: OpenAI returned invalid JSON in tool args",
                    agent_name,
                )
                tool_input = None

        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", 0) or 0
        out_tok = getattr(usage, "completion_tokens", 0) or 0
        # Cached tokens live under prompt_tokens_details when present.
        cache_read = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cache_read = getattr(details, "cached_tokens", 0) or 0
        cost = estimate_openai_cost(model, in_tok, out_tok, cache_read)

        if tool_input is None:
            err = (
                f"OpenAI did not return a function_call (model={model}, "
                f"in={in_tok}, out={out_tok}, finish_reason={finish_reason})."
            )
            _record_run(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                started_at=started,
                completed_at=datetime.utcnow(),
                status=AgentRunStatus.FAILED,
                error_text=err,
            )
            raise RuntimeError(err)

        if cache_read:
            log.info(
                "%s cache: read=%d (~50%% of base cost)",
                agent_name,
                cache_read,
            )

        _record_run(
            proposal_id=proposal_id,
            agent_name=agent_name,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            started_at=started,
            completed_at=datetime.utcnow(),
            status=AgentRunStatus.COMPLETED,
        )
        return tool_input, {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_tokens": 0,
            "cache_read_tokens": cache_read,
            "cost_usd": cost,
            "stop_reason": finish_reason,
        }


def _openai_model_supports_temperature(model: str) -> bool:
    """OpenAI's reasoning-class models (o1 / o3 / o4 series and gpt-5+)
    only accept the default temperature=1. Sending any other value
    returns a 400 BadRequest. Detect those models and skip the temperature
    parameter entirely. All other gpt-*/o-* models accept it normally.
    """
    if model.startswith(("o1", "o3", "o4")):
        return False
    if model.startswith("gpt-5"):
        return False
    return True


_openai_singleton: OpenAISync | None = None


def get_openai() -> OpenAISync:
    global _openai_singleton
    if _openai_singleton is None:
        _openai_singleton = OpenAISync()
    return _openai_singleton


# ---- Provider dispatcher --------------------------------------------------


def call_tool_for_model(
    *,
    model: str,
    system: str | None,
    messages: list[dict[str, Any]],
    tool: dict[str, Any],
    max_tokens: int = 8000,
    agent_name: str,
    proposal_id: int | None = None,
    temperature: float = 0.0,
    cached_prefix: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Route a `call_tool` invocation to the correct provider based on the
    model name's prefix:

    - ``claude-*`` → Anthropic
    - ``gpt-*`` / ``o1-*`` / ``o3-*`` / ``o4-*`` → OpenAI
    - ``gemini-*`` → Google Gemini

    Lets agents declare which model they want via config and not care which
    SDK to call. Same return shape across all providers.
    """
    if model.startswith("claude-"):
        client: Any = get_anthropic()
    elif model.startswith(("gpt-", "o1-", "o3-", "o4-")):
        client = get_openai()
    elif model.startswith("gemini-"):
        client = get_gemini()
    else:
        raise ValueError(
            f"Unknown provider for model {model!r}. Expected a name starting "
            f"with 'claude-', 'gpt-', 'o1-', 'o3-', 'o4-', or 'gemini-'."
        )
    return client.call_tool(
        model=model,
        system=system,
        messages=messages,
        tool=tool,
        max_tokens=max_tokens,
        agent_name=agent_name,
        proposal_id=proposal_id,
        temperature=temperature,
        cached_prefix=cached_prefix,
    )
