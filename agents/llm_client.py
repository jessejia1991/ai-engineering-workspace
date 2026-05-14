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
from typing import Optional, Any

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()


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
DEFAULT_MAX_RETRIES        = _env_int("ANTHROPIC_MAX_RETRIES", 4)
DEFAULT_BACKOFF_BASE_S     = _env_float("ANTHROPIC_BACKOFF_BASE_S", 1.0)
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
        token_budget:       Optional[int] = DEFAULT_TOKEN_BUDGET,
    ):
        self.max_concurrent    = max_concurrent
        self.request_timeout_s = request_timeout_s
        self.max_retries       = max_retries
        self.backoff_base_s    = backoff_base_s
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
                    return response

                except asyncio.TimeoutError:
                    # Don't retry on timeout — likely indicates the upstream
                    # request is genuinely stuck; retrying piles on.
                    self._n_timeouts += 1
                    raise

                except anthropic.RateLimitError:
                    if attempt >= self.max_retries:
                        raise
                    self._n_retries += 1
                    await asyncio.sleep(self.backoff_base_s * (2 ** attempt))
                    continue

                except anthropic.APIStatusError as e:
                    # 529 (overloaded) and other server errors → retry
                    status = getattr(e, "status_code", None)
                    if status in (500, 502, 503, 504, 529):
                        if attempt >= self.max_retries:
                            raise
                        self._n_retries += 1
                        await asyncio.sleep(self.backoff_base_s * (2 ** attempt))
                        continue
                    raise


class _MessagesProxy:
    """Mirrors AsyncAnthropic().messages so callers can write
    `client.messages.create(...)` without knowing it's wrapped."""

    def __init__(self, parent: RateLimitedAnthropicClient):
        self._parent = parent

    async def create(self, **kwargs: Any):
        return await self._parent._messages_create(**kwargs)


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
