"""One-shot connectivity check: each deployed model answers, with latency and token usage."""

import asyncio
import time

from openai import AsyncOpenAI

from agent.config import get_settings


async def main() -> None:
    s = get_settings()
    client = AsyncOpenAI(
        base_url=f"{s.azure_openai_endpoint.rstrip('/')}/openai/v1/",
        api_key=s.azure_openai_api_key,
    )

    for deployment in (s.utility_deployment, s.chat_deployment):
        t0 = time.perf_counter()
        r = await client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_completion_tokens=200,
        )
        ms = (time.perf_counter() - t0) * 1000
        u = r.usage
        print(
            f"{deployment}: {r.choices[0].message.content!r} "
            f"{ms:.0f}ms in={u.prompt_tokens} out={u.completion_tokens}"
        )

    t0 = time.perf_counter()
    e = await client.embeddings.create(model=s.embed_deployment, input=["hello world"])
    ms = (time.perf_counter() - t0) * 1000
    print(f"{s.embed_deployment}: dims={len(e.data[0].embedding)} {ms:.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
