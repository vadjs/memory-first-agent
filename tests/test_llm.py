"""Offline coverage for the LLM module's fragile edges: usage-field probing and
the transport-only retry policy (ADR-0008) — no live keys involved."""

import httpx
import pytest
from openai import RateLimitError

from agent.llm import _retry_transient, _usage_number


class _Details:
    input_token_count = 1200
    output_token_count = 340


def test_usage_number_reads_attribute_style():
    assert _usage_number(_Details(), "input_token_count", "prompt_tokens") == 1200


def test_usage_number_falls_back_across_names():
    assert _usage_number({"prompt_tokens": 55}, "input_token_count", "prompt_tokens") == 55


def test_usage_number_zero_when_absent():
    assert _usage_number(None, "input_token_count") == 0
    assert _usage_number({}, "input_token_count") == 0


def _rate_limit_error() -> RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "http://test"))
    return RateLimitError("rate limited", response=response, body=None)


async def test_transient_errors_are_retried():
    calls = 0

    @_retry_transient
    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _rate_limit_error()
        return "ok"

    assert await flaky() == "ok"
    assert calls == 2


async def test_bad_output_is_never_retried():
    calls = 0

    @_retry_transient
    async def broken():
        nonlocal calls
        calls += 1
        raise ValueError("structured output missing")

    with pytest.raises(ValueError):
        await broken()
    assert calls == 1  # quality failures are the eval suite's territory, not the retry loop's
