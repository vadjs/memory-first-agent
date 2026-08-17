"""LLM services. The conversation role runs on MS Agent Framework (Agent +
OpenAIChatClient against the Azure endpoint); the utility role uses the OpenAI
SDK's typed structured-output parse, which the guardrails depend on (ADR-0003).
Retries cover transport-class failures only — never "bad output" (spec §9)."""

import asyncio

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from agent.aoai import base_url_for, client_for
from agent.config import Settings
from agent.prompts import SYNTHESIS_SYSTEM
from agent.telemetry import Usage

_TRANSIENT = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    TimeoutError,
)

_retry_transient = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=8),
    retry=retry_if_exception_type(_TRANSIENT),
    reraise=True,
)


def _usage_number(details, *names) -> int:
    for name in names:
        value = getattr(details, name, None)
        if value is None and isinstance(details, dict):
            value = details.get(name)
        if value is not None:
            return int(value)
    return 0


class ConversationLLM:
    """Grounded synthesis on the chat model, minimal reasoning for chat latency."""

    def __init__(self, settings: Settings):
        self._settings = settings
        base_url = base_url_for(settings)
        if settings.use_managed_identity:
            from azure.identity import DefaultAzureCredential

            client = OpenAIChatClient(
                model=settings.chat_deployment,
                azure_endpoint=settings.azure_openai_endpoint,
                credential=DefaultAzureCredential(),
            )
        else:
            client = OpenAIChatClient(
                model=settings.chat_deployment,
                api_key=settings.azure_openai_api_key,
                base_url=base_url,
            )
        # OpenAIChatClient speaks the Responses API: reasoning effort is nested there.
        self._agent = Agent(
            # agent-framework's own OpenAIChatClient fails its Supports* protocol generics
            client=client,  # ty: ignore[invalid-argument-type]
            instructions=SYNTHESIS_SYSTEM,
            default_options={"reasoning": {"effort": "none"}},
        )

    @_retry_transient
    async def synthesize(self, user_message: str) -> tuple[str, Usage]:
        response = await asyncio.wait_for(
            self._agent.run(user_message), timeout=self._settings.llm_timeout_s
        )
        details = getattr(response, "usage_details", None)
        usage = Usage(
            self._settings.chat_deployment,
            _usage_number(details, "input_token_count", "prompt_tokens"),
            _usage_number(details, "output_token_count", "completion_tokens"),
        )
        return response.text, usage


class UtilityLLM:
    """Classification, screening, and tagging on the small model with typed JSON output."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: AsyncOpenAI = client_for(settings)

    @_retry_transient
    async def complete_json[SchemaT: BaseModel](
        self, system: str, user: str, schema: type[SchemaT]
    ) -> tuple[SchemaT, Usage]:
        response = await self._client.chat.completions.parse(
            model=self._settings.utility_deployment,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
            reasoning_effort="minimal",
            timeout=self._settings.utility_timeout_s,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError("structured output missing")
        u = response.usage
        usage = Usage(
            self._settings.utility_deployment,
            u.prompt_tokens if u else 0,
            u.completion_tokens if u else 0,
        )
        return parsed, usage
