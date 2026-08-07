"""Two-tier Redis vector memory: the Answer Cache and the Knowledge Base.

Keys are content hashes, so every write is idempotent and convergent (ADR-0002).
Freshness is enforced by the caller at read time (freshness-as-routing, spec §5.3);
this module stores timestamps and never expires vector entries on its own.
"""

import hashlib
import json
import re
import struct
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from redis import ResponseError
from redis.asyncio import Redis
from redis.commands.search.field import NumericField, TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from agent.embeddings import Embedder

_TRACKING_PARAMS = re.compile(r"^(utm_|gclid|fbclid|mc_eid|ref$)", re.IGNORECASE)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_question(question: str) -> str:
    return normalize_text(question).lower()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), "")
    )


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def to_similarity(cosine_distance: float) -> float:
    """The one place Redis cosine *distance* becomes similarity (ADR-0004)."""
    return 1.0 - cosine_distance


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _s(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


@dataclass
class ChunkHit:
    key: str
    text: str
    url: str
    title: str
    section: str
    fetched_at: float
    similarity: float


@dataclass
class CacheHit:
    key: str
    question: str
    answer: str
    urls: list[str]
    topic: str
    temporal: str
    created_at: float
    similarity: float


class MemoryStore:
    def __init__(
        self,
        redis_url: str,
        embedder: Embedder,
        dim: int | None = None,
        namespace: str = "mfa",
    ):
        self.r: Redis = Redis.from_url(redis_url, decode_responses=False)
        self.embedder = embedder
        self.dim = dim or embedder.dim
        self.ns = namespace
        self.chunk_prefix = f"{namespace}:chunk:"
        self.qa_prefix = f"{namespace}:qa:"
        self.url_prefix = f"{namespace}:url:"
        self.chunk_index = f"idx:{namespace}:chunks"
        self.qa_index = f"idx:{namespace}:qa"

    # -- setup ---------------------------------------------------------------

    async def ensure_indexes(self) -> None:
        vector_schema = {
            "TYPE": "FLOAT32",
            "DIM": self.dim,
            "DISTANCE_METRIC": "COSINE",
        }
        for index, prefix, fields in (
            (
                self.chunk_index,
                self.chunk_prefix,
                [
                    TagField("quarantined"),
                    NumericField("fetched_at"),
                    TextField("url", no_stem=True),
                    VectorField("vec", "FLAT", vector_schema),
                ],
            ),
            (
                self.qa_index,
                self.qa_prefix,
                [
                    NumericField("created_at"),
                    VectorField("vec", "FLAT", vector_schema),
                ],
            ),
        ):
            try:
                await self.r.ft(index).create_index(
                    fields,
                    definition=IndexDefinition(prefix=[prefix], index_type=IndexType.HASH),
                )
            except ResponseError as e:
                if "already exists" not in str(e).lower():
                    raise

    # -- reads ---------------------------------------------------------------

    async def search_cache(self, query: str) -> CacheHit | None:
        vec = (await self.embedder.embed([normalize_question(query)]))[0]
        q = (
            Query("*=>[KNN 1 @vec $B AS dist]")
            .sort_by("dist")
            .return_fields("question", "answer", "urls", "topic", "temporal", "created_at", "dist")
            .dialect(2)
        )
        res = await self.r.ft(self.qa_index).search(q, {"B": _pack(vec)})
        if not res.docs:
            return None
        d = res.docs[0]
        return CacheHit(
            key=_s(d.id),
            question=_s(d.question),
            answer=_s(d.answer),
            urls=json.loads(_s(d.urls) or "[]"),
            topic=_s(d.topic),
            temporal=_s(d.temporal),
            created_at=float(_s(d.created_at) or 0),
            similarity=to_similarity(float(_s(d.dist))),
        )

    async def search_chunks(self, query: str, k: int) -> list[ChunkHit]:
        vec = (await self.embedder.embed([normalize_text(query)]))[0]
        q = (
            Query(f"(@quarantined:{{0}})=>[KNN {k} @vec $B AS dist]")
            .sort_by("dist")
            .return_fields("text", "url", "title", "section", "fetched_at", "dist")
            .dialect(2)
        )
        res = await self.r.ft(self.chunk_index).search(q, {"B": _pack(vec)})
        return [
            ChunkHit(
                key=_s(d.id),
                text=_s(d.text),
                url=_s(d.url),
                title=_s(d.title),
                section=_s(d.section),
                fetched_at=float(_s(d.fetched_at) or 0),
                similarity=to_similarity(float(_s(d.dist))),
            )
            for d in res.docs
        ]

    # -- writes --------------------------------------------------------------

    async def upsert_chunks(self, chunks: list[dict]) -> int:
        """Store chunks idempotently. Quarantined chunks are stored without a
        vector: present for audit, invisible to retrieval (spec §5.5)."""
        clean = [c for c in chunks if not c.get("quarantined")]
        vectors = (
            await self.embedder.embed([normalize_text(c["text"]) for c in clean]) if clean else []
        )
        vec_by_id = {id(c): v for c, v in zip(clean, vectors, strict=True)}
        now = time.time()
        pipe = self.r.pipeline(transaction=False)
        for c in chunks:
            key = self.chunk_prefix + sha(normalize_text(c["text"]))
            mapping: dict = {
                "text": c["text"],
                "url": normalize_url(c.get("url", "")),
                "title": c.get("title", ""),
                "section": c.get("section", ""),
                "fetched_at": now,
                "quarantined": "1" if c.get("quarantined") else "0",
            }
            if id(c) in vec_by_id:
                mapping["vec"] = _pack(vec_by_id[id(c)])
            pipe.hset(key, mapping=mapping)
        await pipe.execute()
        return len(chunks)

    async def put_qa(
        self, question: str, answer: str, urls: list[str], topic: str, temporal: str
    ) -> None:
        norm = normalize_question(question)
        vec = (await self.embedder.embed([norm]))[0]
        await self.r.hset(
            self.qa_prefix + sha(norm),
            mapping={
                "question": question,
                "answer": answer,
                "urls": json.dumps(urls),
                "topic": topic,
                "temporal": temporal,
                "created_at": time.time(),
                "vec": _pack(vec),
            },
        )

    async def mark_url_ingested(self, url: str, ttl_days: int) -> None:
        await self.r.set(
            self.url_prefix + sha(normalize_url(url)), str(time.time()), ex=ttl_days * 86400
        )

    async def url_recently_ingested(self, url: str) -> bool:
        return bool(await self.r.exists(self.url_prefix + sha(normalize_url(url))))

    # -- erasure & lifecycle (GDPR, spec §8.6) --------------------------------

    async def forget_url(self, url: str) -> int:
        """Cascade-delete everything derived from a URL, via provenance metadata."""
        target = normalize_url(url)
        deleted = 0
        async for key in self.r.scan_iter(match=f"{self.chunk_prefix}*"):
            if _s(await self.r.hget(key, "url")) == target:
                deleted += await self.r.delete(key)
        async for key in self.r.scan_iter(match=f"{self.qa_prefix}*"):
            urls = json.loads(_s(await self.r.hget(key, "urls")) or "[]")
            if target in (normalize_url(u) for u in urls):
                deleted += await self.r.delete(key)
        deleted += await self.r.delete(self.url_prefix + sha(target))
        return deleted

    async def forget_question(self, question: str) -> int:
        return await self.r.delete(self.qa_prefix + sha(normalize_question(question)))

    async def cleanup(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        deleted = 0
        for prefix, ts_field in ((self.chunk_prefix, "fetched_at"), (self.qa_prefix, "created_at")):
            async for key in self.r.scan_iter(match=f"{prefix}*"):
                ts = float(_s(await self.r.hget(key, ts_field)) or 0)
                if ts < cutoff:
                    deleted += await self.r.delete(key)
        return deleted

    async def stats(self) -> dict:
        counts = {"chunks": 0, "quarantined": 0, "qa": 0}
        async for key in self.r.scan_iter(match=f"{self.chunk_prefix}*"):
            counts["chunks"] += 1
            if _s(await self.r.hget(key, "quarantined")) == "1":
                counts["quarantined"] += 1
        async for _ in self.r.scan_iter(match=f"{self.qa_prefix}*"):
            counts["qa"] += 1
        info = await self.r.info("memory")
        counts["redis_memory"] = _s(info.get("used_memory_human", ""))
        return counts

    async def clear(self) -> int:
        deleted = 0
        async for key in self.r.scan_iter(match=f"{self.ns}:*"):
            deleted += await self.r.delete(key)
        return deleted

    async def ping(self) -> bool:
        return bool(await self.r.ping())

    async def aclose(self) -> None:
        await self.r.aclose()
