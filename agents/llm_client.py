"""
RateLimitedAnthropicClient — drop-in HTTP-layer wrapper around AsyncAnthropic.

Why this exists (P3): every other module in this codebase ultimately calls
`client.messages.create(...)`. With multiple agents fanning out concurrently
(P1 review, P4 multi-expert plan), we need *session-wide* invariants on the
underlying HTTP traffic: concurrency cap, retry on 429/529, per-request
timeout, optional token budget. This wrapper mimics the AsyncAnthropic
surface (`client.messages.create(...)` still async, same kwargs) so existing
call sites stay unchanged.

Shared module-level instance below — every importer gets the same semaphore
and the same accumulating usage counter.
"""

import os
import asyncio
import time
import uuid
import json
import contextvars
from typing import Optional, Any

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()


# --- Trace context (observability slice) -------------------------------
#
# Caller sets these via `set_trace_context(...)` before invoking the LLM;
# the wrapper reads them off the contextvars when writing the observation
# row. ContextVars propagate automatically across `await` and `asyncio.gather`,
# so each concurrent agent gets its own isolated trace context.
#
# If no context is set (e.g., a smoke test calling client.messages.create
# directly), the wrapper still does the API call but skips the DB write —
# observations are best-effort, never load-bearing for the actual response.

_trace_id_var:     contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace_id", default=None)
_agent_name_var:   contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("agent_name", default=None)
_parent_obs_var:   contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("parent_observation_id", default=None)
# Set by `trace replay` to link a replayed generation back to its source obs.
# Read by the wrapper's auto-write path; left None for normal agent calls.
_replayed_from_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("replayed_from_id", default=None)


_UNSET: Any = object()


def set_trace_context(
    trace_id: Any = _UNSET,
    agent_name: Any = _UNSET,
    parent_observation_id: Any = _UNSET,
    replayed_from_id: Any = _UNSET,
) -> dict:
    """
    Set selected fields of the current trace context. Pass only the fields
    you want to override — unspecified fields stay at their current value.
    Returns a dict of contextvar tokens; restore previous state with
    `reset_trace_context(tokens)`.

    Typical usage:
      - Orchestrator entry sets `trace_id` + `agent_name='Orchestrator'`.
      - Per-agent `review()` overrides just `agent_name=self.name`,
        inheriting the orchestrator's trace_id.

    Each agent runs in its own asyncio.Task (via asyncio.gather), so
    contextvars copies isolate concurrent agents automatically — callers
    do not have to reset unless they want strict scoping inside a single
    task.
    """
    tokens: dict = {}
    if trace_id is not _UNSET:
        tokens["trace_id"] = _trace_id_var.set(trace_id)
    if agent_name is not _UNSET:
        tokens["agent_name"] = _agent_name_var.set(agent_name)
    if parent_observation_id is not _UNSET:
        tokens["parent_observation_id"] = _parent_obs_var.set(parent_observation_id)
    if replayed_from_id is not _UNSET:
        tokens["replayed_from_id"] = _replayed_from_var.set(replayed_from_id)
    return tokens


def reset_trace_context(tokens: dict) -> None:
    if "trace_id" in tokens:
        _trace_id_var.reset(tokens["trace_id"])
    if "agent_name" in tokens:
        _agent_name_var.reset(tokens["agent_name"])
    if "parent_observation_id" in tokens:
        _parent_obs_var.reset(tokens["parent_observation_id"])
    if "replayed_from_id" in tokens:
        _replayed_from_var.reset(tokens["replayed_from_id"])


# --- Configuration via env, with sane defaults --------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_opt_int(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


DEFAULT_MAX_CONCURRENT     = _env_int("ANTHROPIC_MAX_CONCURRENT", 5)
DEFAULT_REQUEST_TIMEOUT_S  = _env_float("ANTHROPIC_REQUEST_TIMEOUT_S", 120.0)
DEFAULT_MAX_RETRIES        = _env_int("ANTHROPIC_MAX_RETRIES", 6)        # bumped from 4 after PR #4533 hit rate limit at 4
DEFAULT_BACKOFF_BASE_S     = _env_float("ANTHROPIC_BACKOFF_BASE_S", 2.0) # bumped from 1.0 — anthropic 429 reset is typically 60s, not 15s
DEFAULT_BACKOFF_MAX_S      = _env_float("ANTHROPIC_BACKOFF_MAX_S", 60.0) # cap so we don't sleep forever on exponential
DEFAULT_TOKEN_BUDGET       = _env_opt_int("ANTHROPIC_TOKEN_BUDGET")


class BudgetExceeded(Exception):
    """Raised before a request goes out if it would push usage past the
    configured token_budget. Pre-emptive — no wasted API call."""


# --- The client --------------------------------------------------------

class RateLimitedAnthropicClient:
    """
    Drop-in for AsyncAnthropic. Use it the same way:
        response = await client.messages.create(model=..., messages=[...])

    Extra behavior:
      - At most `max_concurrent` in-flight requests at any moment
      - Per-request timeout (raises asyncio.TimeoutError on hang)
      - Exponential backoff on 429 / 529 (RateLimitError / status 529)
      - Optional token_budget: raises BudgetExceeded *before* the next call
        if cumulative input+output tokens would exceed the cap
      - usage_summary() returns observability data for CLI display
    """

    def __init__(
        self,
        max_concurrent:     int   = DEFAULT_MAX_CONCURRENT,
        request_timeout_s:  float = DEFAULT_REQUEST_TIMEOUT_S,
        max_retries:        int   = DEFAULT_MAX_RETRIES,
        backoff_base_s:     float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s:      float = DEFAULT_BACKOFF_MAX_S,
        token_budget:       Optional[int] = DEFAULT_TOKEN_BUDGET,
    ):
        self.max_concurrent    = max_concurrent
        self.request_timeout_s = request_timeout_s
        self.max_retries       = max_retries
        self.backoff_base_s    = backoff_base_s
        self.backoff_max_s     = backoff_max_s
        self.token_budget      = token_budget

        # Disable SDK-level retry so this wrapper has sole control over
        # retry policy and observability.
        self._sdk = AsyncAnthropic(max_retries=0)

        self._semaphore = asyncio.Semaphore(max_concurrent)

        # Accumulating session counters
        self._n_requests      = 0
        self._n_retries       = 0
        self._n_timeouts      = 0
        self._n_budget_blocks = 0
        self._input_tokens    = 0
        self._output_tokens   = 0

        # Mimic AsyncAnthropic.messages namespace
        self.messages = _MessagesProxy(self)

    # ----- public observability -----

    def usage_summary(self) -> dict:
        return {
            "requests":        self._n_requests,
            "retries":         self._n_retries,
            "timeouts":        self._n_timeouts,
            "budget_blocks":   self._n_budget_blocks,
            "input_tokens":    self._input_tokens,
            "output_tokens":   self._output_tokens,
            "budget_used":     self._input_tokens + self._output_tokens,
            "budget_cap":      self.token_budget,
            "max_concurrent":  self.max_concurrent,
        }

    def reset_usage(self) -> None:
        """Zero out counters — useful between CLI commands inside one process."""
        self._n_requests      = 0
        self._n_retries       = 0
        self._n_timeouts      = 0
        self._n_budget_blocks = 0
        self._input_tokens    = 0
        self._output_tokens   = 0

    # ----- internal call path -----

    async def _messages_create(self, **kwargs: Any):
        # Budget pre-check: refuse the request before sending if we are
        # already past the cap. Note we don't know the cost of the next
        # request in advance, so we check the post-condition optimistically
        # (i.e., "we already overshot — stop").
        if self.token_budget is not None:
            used = self._input_tokens + self._output_tokens
            if used >= self.token_budget:
                self._n_budget_blocks += 1
                raise BudgetExceeded(
                    f"token_budget {self.token_budget} already used "
                    f"({used} tokens consumed); refusing new request"
                )

        # Capture trace context once at call entry — even if retries happen,
        # one logical call == one observation row. observation_id is generated
        # here so a hypothetical caller could read it back, but we mostly
        # discover it later via the observations table.
        observation_id  = "obs-" + uuid.uuid4().hex[:12]
        trace_id        = _trace_id_var.get()
        agent_name      = _agent_name_var.get()
        parent_obs_id   = _parent_obs_var.get()
        replayed_from   = _replayed_from_var.get()

        start_perf = time.perf_counter()
        response: Any = None
        err: Optional[BaseException] = None

        try:
            async with self._semaphore:
                for attempt in range(self.max_retries + 1):
                    try:
                        response = await asyncio.wait_for(
                            self._sdk.messages.create(**kwargs),
                            timeout=self.request_timeout_s,
                        )
                        self._n_requests += 1
                        usage = getattr(response, "usage", None)
                        if usage is not None:
                            self._input_tokens  += getattr(usage, "input_tokens",  0) or 0
                            self._output_tokens += getattr(usage, "output_tokens", 0) or 0
                        break  # success — fall through to observation write

                    except asyncio.TimeoutError:
                        # Don't retry on timeout — likely indicates the upstream
                        # request is genuinely stuck; retrying piles on.
                        self._n_timeouts += 1
                        raise

                    except anthropic.RateLimitError as e:
                        if attempt >= self.max_retries:
                            raise
                        self._n_retries += 1
                        await asyncio.sleep(self._backoff_for(attempt, e))
                        continue

                    except anthropic.APIStatusError as e:
                        # 529 (overloaded) and other server errors → retry
                        status = getattr(e, "status_code", None)
                        if status in (500, 502, 503, 504, 529):
                            if attempt >= self.max_retries:
                                raise
                            self._n_retries += 1
                            await asyncio.sleep(self._backoff_for(attempt, e))
                            continue
                        raise
        except BaseException as e:
            err = e
            raise
        finally:
            if trace_id:  # observations are best-effort; only write when a trace is active
                latency_ms = int((time.perf_counter() - start_perf) * 1000)
                await _write_observation_safe(
                    observation_id=observation_id,
                    trace_id=trace_id,
                    parent_observation_id=parent_obs_id,
                    agent_name=agent_name,
                    request_kwargs=kwargs,
                    response=response,
                    latency_ms=latency_ms,
                    error=err,
                    replayed_from_id=replayed_from,
                )

        return response

    def _backoff_for(self, attempt: int, error: Exception) -> float:
        """
        Compute sleep before retry. Prefers the server's Retry-After hint
        when present (Anthropic includes one in some 429s); falls back to
        exponential `base * 2**attempt`, capped at `backoff_max_s`.
        """
        # Look for a Retry-After hint on the response, if any.
        # anthropic SDK exposes the underlying response on the error object.
        retry_after = None
        try:
            resp = getattr(error, "response", None)
            if resp is not None:
                hdr = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
                if hdr:
                    retry_after = float(hdr)
        except Exception:
            pass
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.backoff_max_s)
        return min(self.backoff_base_s * (2 ** attempt), self.backoff_max_s)


class _MessagesProxy:
    """Mirrors AsyncAnthropic().messages so callers can write
    `client.messages.create(...)` without knowing it's wrapped."""

    def __init__(self, parent: RateLimitedAnthropicClient):
        self._parent = parent

    async def create(self, **kwargs: Any):
        return await self._parent._messages_create(**kwargs)


async def _write_observation_safe(
    *,
    observation_id: str,
    trace_id: str,
    parent_observation_id: Optional[str],
    agent_name: Optional[str],
    request_kwargs: dict,
    response: Any,
    latency_ms: int,
    error: Optional[BaseException],
    replayed_from_id: Optional[str] = None,
) -> None:
    """
    Persist one `generation` observation. Imports database lazily because
    `agents.llm_client` is imported before `database` in some call paths,
    and observations should never crash the actual LLM call — any failure
    inside this function is swallowed.

    `request_kwargs` is the full dict the caller passed to messages.create
    (system + messages + model + tools + max_tokens + temperature + ...).
    Storing the whole kwargs blob makes `trace replay` trivial: load JSON,
    edit, send back through the same wrapper.
    """
    try:
        from database import save_observation  # late import (cycle-safe)

        try:
            messages_json = json.dumps(request_kwargs, default=str)
        except Exception:
            messages_json = json.dumps({"_unserializable": True, "model": request_kwargs.get("model")})

        response_json: Optional[str] = None
        input_tokens: Optional[int]  = None
        output_tokens: Optional[int] = None
        finish_reason: Optional[str] = None

        if response is not None:
            # Anthropic SDK is pydantic v2; model_dump_json() captures all fields.
            try:
                response_json = response.model_dump_json()
            except Exception:
                try:
                    response_json = json.dumps(response.model_dump(), default=str)
                except Exception:
                    response_json = json.dumps({"_unserializable": True})
            usage = getattr(response, "usage", None)
            if usage is not None:
                input_tokens  = getattr(usage, "input_tokens",  None)
                output_tokens = getattr(usage, "output_tokens", None)
            finish_reason = getattr(response, "stop_reason", None)

        err_msg: Optional[str] = None
        if error is not None:
            err_msg = f"{type(error).__name__}: {error}"
            # On failure we still record the call but flag the finish_reason.
            if finish_reason is None:
                finish_reason = "error"

        await save_observation(
            observation_id=observation_id,
            trace_id=trace_id,
            parent_observation_id=parent_observation_id,
            type="generation",
            agent_name=agent_name,
            model=request_kwargs.get("model"),
            provider="anthropic",
            operation="chat",
            messages_json=messages_json,
            response_json=response_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            error_message=err_msg,
            replayed_from_id=replayed_from_id,
        )
    except Exception:
        # Observability must never break the actual call path. Swallow.
        pass


def format_usage_summary(usage: dict) -> str:
    """
    One-line summary for CLI display. Pure string — does not depend on
    Rich, so callers can wrap or color as they prefer.
    """
    parts = [
        f"{usage['requests']} req",
        f"{usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens",
    ]
    if usage.get("retries"):
        parts.append(f"{usage['retries']} retries")
    if usage.get("timeouts"):
        parts.append(f"{usage['timeouts']} timeouts")
    if usage.get("budget_blocks"):
        parts.append(f"{usage['budget_blocks']} budget blocks")
    if usage.get("budget_cap"):
        parts.append(f"budget {usage['budget_used']:,}/{usage['budget_cap']:,}")
    return " · ".join(parts)


# --- Shared module-level singleton -------------------------------------

# Every importer gets the same semaphore and usage counter. This is the
# whole point: a global concurrency cap + global budget per session.
client = RateLimitedAnthropicClient()
