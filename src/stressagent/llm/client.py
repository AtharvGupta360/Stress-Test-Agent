"""Gemini client: structured output, per-submission budget, circuit breaker.

The model only ever *proposes*. Nothing it returns reaches the user without
first surviving execution in the sandbox, so the job of this module is narrow:
get well-formed structured output, never exceed the budget, and fail loudly
enough that the pipeline can degrade instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from ..config import settings
from ..db import add_usage, log_step

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE_MARKERS = ("429", "500", "502", "503", "504", "deadline", "unavailable", "overloaded")


class ModelUnavailable(RuntimeError):
    """API is down or the breaker is open -> degrade to verdict-only."""


class BudgetExhausted(RuntimeError):
    """Submission hit its token / call ceiling -> degrade."""


class MalformedResponse(RuntimeError):
    """Model returned something that will not parse against the schema."""


# ------------------------------------------------------------ circuit breaker


class CircuitBreaker:
    """Process-wide. Once the model API starts failing, every submission will
    hit the same wall, so there is no point in each of them discovering it
    independently -- they should all degrade immediately and cheaply."""

    def __init__(self, threshold: int, reset_seconds: int) -> None:
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.failures = 0
        self.opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if self.failures < self.threshold:
            return False
        if time.monotonic() - self.opened_at > self.reset_seconds:
            # Half-open: allow one probe through. If it fails, record_failure
            # re-opens the breaker for another window.
            self.failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


_breaker: CircuitBreaker | None = None


def breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        s = settings()
        _breaker = CircuitBreaker(s.breaker_fail_threshold, s.breaker_reset_seconds)
    return _breaker


# -------------------------------------------------------------------- client

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        s = settings()
        if not s.gemini_api_key:
            raise ModelUnavailable("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=s.gemini_api_key)
    return _client


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


async def generate(
    submission_id: str,
    *,
    stage: str,
    system: str,
    prompt: str,
    schema: type[T],
    temperature: float = 0.3,
    max_output_tokens: int = 8192,
    attempts: int = 3,
) -> T:
    """One structured-output call, budgeted and logged.

    Raises ModelUnavailable / BudgetExhausted / MalformedResponse -- the caller
    maps all three onto DEGRADED rather than failing the submission outright,
    because the compile-and-judge verdict is still worth returning.
    """
    s = settings()

    if breaker().is_open:
        raise ModelUnavailable("circuit breaker open")

    usage = await add_usage(submission_id, 0)  # read-modify: counts this call
    if usage["llm_calls"] > s.max_llm_calls_per_submission:
        raise BudgetExhausted(f"llm call cap ({s.max_llm_calls_per_submission}) reached")
    if usage["tokens_used"] > s.max_tokens_per_submission:
        raise BudgetExhausted(f"token cap ({s.max_tokens_per_submission}) reached")

    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        response_schema=schema,
        # Generated brute-force code for problems about, say, combat damage or
        # network attacks trips the default safety thresholds. This is code
        # generation over a problem statement, so the filters are pure noise.
        safety_settings=[
            types.SafetySetting(category=c, threshold="BLOCK_NONE")
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    )

    last_error: Exception | None = None
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            resp = await client().aio.models.generate_content(
                model=s.gemini_model, contents=prompt, config=config
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide range
            last_error = exc
            text = str(exc).lower()
            if any(m in text for m in RETRYABLE_MARKERS) and attempt < attempts - 1:
                await asyncio.sleep(2**attempt)
                continue
            breaker().record_failure()
            await log_step(
                submission_id, stage=stage, kind="llm_call", status="fail",
                payload={"attempt": attempt}, output={"error": str(exc)[:2000]},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise ModelUnavailable(str(exc)) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        tokens = getattr(resp, "usage_metadata", None)
        total_tokens = getattr(tokens, "total_token_count", 0) or 0
        await add_usage(submission_id, total_tokens)

        # A truncated response is *not* a model failure -- it parses as invalid
        # JSON and would otherwise burn all three attempts on the same cliff.
        finish = ""
        if resp.candidates:
            finish = str(getattr(resp.candidates[0], "finish_reason", "") or "")
        if "MAX_TOKENS" in finish.upper():
            last_error = MalformedResponse("response truncated at max_output_tokens")
            if attempt < attempts - 1:
                config.max_output_tokens = min(int(max_output_tokens * 2), 32768)
                continue

        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, schema):
            breaker().record_success()
            await log_step(
                submission_id, stage=stage, kind="llm_call", status="ok",
                payload={"attempt": attempt, "model": s.gemini_model},
                output={"finish_reason": finish}, tokens=total_tokens,
                duration_ms=duration_ms,
            )
            return parsed

        # Fall back to manual parsing: `parsed` is None whenever the schema was
        # only partially honoured, but the text is usually still recoverable.
        try:
            data = json.loads(_strip_fence(resp.text or ""))
            obj = schema.model_validate(data)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            last_error = MalformedResponse(str(exc))
            if attempt < attempts - 1:
                await asyncio.sleep(1)
                continue
        else:
            breaker().record_success()
            await log_step(
                submission_id, stage=stage, kind="llm_call", status="ok",
                payload={"attempt": attempt, "recovered": True},
                output={"finish_reason": finish}, tokens=total_tokens,
                duration_ms=duration_ms,
            )
            return obj

    await log_step(
        submission_id, stage=stage, kind="llm_call", status="fail",
        output={"error": str(last_error)[:2000]},
    )
    raise MalformedResponse(str(last_error))
