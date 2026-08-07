"""HTTP API — the production entrypoint (spec §12). Chat and analytics only;
admin verbs (memory, erasure) are deliberately CLI-only."""

import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from agent.analytics import summarize
from agent.config import Settings


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


class ChatOut(BaseModel):
    answer: str
    route: str
    sources: list[dict]
    turn_id: str
    session_id: str


def create_app(pipeline, sessions, settings: Settings, limiter) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await pipeline.memory.ensure_indexes()
        except Exception:
            pass  # Redis may be down at boot; /healthz reports it
        yield

    api = FastAPI(title="memory-first-agent", lifespan=lifespan)

    async def authed(request: Request) -> str:
        if not settings.api_key:
            return "anonymous"
        header = request.headers.get("authorization", "")
        if header != f"Bearer {settings.api_key}":
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")
        return settings.api_key

    async def rate_limited(request: Request, caller: str = Depends(authed)) -> str:
        key = caller if caller != "anonymous" else (request.client.host if request.client else "?")
        if not await limiter.allow(key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return caller

    @api.post("/chat", response_model=ChatOut)
    async def chat(body: ChatIn, _: str = Depends(rate_limited)) -> ChatOut:
        if len(body.message) > settings.max_query_chars:
            raise HTTPException(
                status_code=422,
                detail=f"message exceeds {settings.max_query_chars} characters",
            )
        session_id = body.session_id or uuid.uuid4().hex[:16]
        history = await sessions.get(session_id)
        result = await pipeline.answer_turn(body.message, history, session_id=session_id)
        await sessions.append(session_id, body.message, result.answer)
        return ChatOut(
            answer=result.answer,
            route=result.route,
            sources=result.sources,
            turn_id=result.record.turn_id,
            session_id=session_id,
        )

    @api.get("/analytics/summary")
    async def analytics(_: str = Depends(rate_limited)) -> dict:
        return summarize()

    @api.get("/healthz")
    async def healthz() -> dict:
        try:
            await pipeline.memory.ping()
        except Exception as e:
            raise HTTPException(status_code=503, detail="redis unreachable") from e
        return {"status": "ok"}

    return api


def _default_app() -> FastAPI:
    from agent.cli import _build
    from agent.ratelimit import TokenBucket
    from agent.sessions import SessionStore

    settings, memory, pipeline = _build()
    return create_app(
        pipeline,
        SessionStore(memory.r),
        settings,
        TokenBucket(memory.r, settings.rate_limit_per_min),
    )


app = _default_app()
