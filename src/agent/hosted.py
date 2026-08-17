"""Foundry Hosted Agent adapter (ADR-0009).

Exposes the memory-first Pipeline through the agent-framework chat-client protocol,
so Foundry's Responses host serves it unchanged: the platform owns sessions and
transport; the routing logic stays exactly the code that runs everywhere else.
Conversation history arrives in the protocol's message sequence — hosted mode
needs no separate session store."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Message,
    ResponseStream,
)

from agent.domain import QueryTooLongError

try:
    # Vendor-private; only silences a per-request capability warning. A framework
    # upgrade that moves it must degrade to the warning, not break the import.
    from agent_framework._tools import FunctionInvocationLayer
except ImportError:  # pragma: no cover

    class FunctionInvocationLayer:  # type: ignore[no-redef]
        pass


def _message_text(message: Any) -> str:
    text = getattr(message, "text", None)
    if text:
        return text
    if isinstance(message, Mapping):
        return str(message.get("content", ""))
    return str(message)


def _message_role(message: Any) -> str:
    role = getattr(message, "role", None) or (
        message.get("role") if isinstance(message, Mapping) else "user"
    )
    return str(getattr(role, "value", role))


class PipelineChatClient(FunctionInvocationLayer, BaseChatClient):  # ty: ignore[unsupported-base]
    """The one abstract hook agent-framework requires; both modes funnel through
    a single answer_turn call, so hosted behavior can never drift from the API/CLI.

    FunctionInvocationLayer is mixed in with no tools registered: it makes the
    client a first-class citizen of Agent's capability checks (silencing a
    per-request warning) while the pipeline remains deliberately tool-free
    (least privilege, spec §8 layer 5)."""

    def __init__(self, pipeline, **kwargs: Any):
        super().__init__(**kwargs)
        self._pipeline = pipeline

    async def _answer(self, messages: Sequence[Message]) -> ChatResponse:
        turns = [(_message_role(m), _message_text(m)) for m in messages]
        user_turns = [t for t in turns if t[0] == "user"]
        query = user_turns[-1][1] if user_turns else ""
        history = [
            {"role": role, "content": text}
            for role, text in turns[: -1 if user_turns else None]
            if role in ("user", "assistant") and text
        ]
        try:
            result = await self._pipeline.answer_turn(query, history)
        except QueryTooLongError as e:
            # Same translation the API (422) and CLI perform: a clean rejection
            # message, not a protocol-level 500 out of the Responses host.
            return ChatResponse(messages=Message(role="assistant", contents=[str(e)]))
        answer = result.answer
        if result.sources:
            answer += "\n\nSources:\n" + "\n".join(s["url"] for s in result.sources)
        return ChatResponse(messages=Message(role="assistant", contents=[answer]))

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ):
        if not stream:
            return self._answer(messages)

        async def updates() -> AsyncIterator[ChatResponseUpdate]:
            response = await self._answer(messages)
            yield ChatResponseUpdate(contents=list(response.messages[0].contents), role="assistant")

        def finalize(collected: Sequence[ChatResponseUpdate]) -> ChatResponse:
            contents = [c for u in collected for c in (u.contents or [])]
            return ChatResponse(messages=Message(role="assistant", contents=contents))

        return ResponseStream(updates(), finalizer=finalize)


def build_hosted_agent(client: PipelineChatClient) -> Agent:
    return Agent(
        client=client,
        # Deterministic id for local/self-hosted runs. In Foundry hosting the
        # platform stamps its own stable agent GUID into gen_ai.agent.id.
        id="memory-first-agent",
        name="memory-first-agent",
        description=(
            "Answers questions memory-first from a two-tier Redis vector memory, "
            "falling back to live web search on a miss; every answer cites its sources."
        ),
    )
