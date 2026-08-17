"""Two-tier Redis vector memory: the Answer Cache and the Knowledge Base.

Keys are content hashes, so every write is idempotent and convergent (ADR-0002).
Freshness is enforced by the caller at read time (freshness-as-routing, spec §5.3);
this module stores timestamps and never expires vector entries on its own.

Reads and writes that embed return their token usage in-band; the vector packing,
key schema, and field names never leave this module.
"""

import hashlib
import json
import re
import struct
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from redis import ResponseError
from redis.asyncio import Redis
from redis.commands.search.field import Field, NumericField, TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from agent.embeddings import Embedder
from agent.telemetry import Usage, log

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


def _unpack(raw: bytes) -> list[float]:
    return list(struct.unpack(f"{len(raw) // 4}f", raw))


def _s(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


@dataclass
class ChunkRecord:
    """One chunk as written to the Knowledge Base — the typed seam between
    ingestion and storage."""

    text: str
    url: str
    title: str = ""
    section: str = ""
    quarantined: bool = False
    pos: int = 0  # document order within the page (summary = 0)


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
        indexes: list[tuple[str, str, list[Field]]] = [
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
        ]
        for index, prefix, fields in indexes:
            try:
                await self.r.ft(index).create_index(
                    fields,
                    definition=IndexDefinition(prefix=[prefix], index_type=IndexType.HASH),
                )
            except ResponseError as e:
                if "already exists" not in str(e).lower():
                    raise

    # -- reads ---------------------------------------------------------------

    async def search_cache(self, query: str) -> tuple[CacheHit | None, Usage | None]:
        vectors, usage = await self.embedder.embed([normalize_question(query)])
        q = (
            Query("*=>[KNN 1 @vec $B AS dist]")
            .sort_by("dist")
            .return_fields("question", "answer", "urls", "topic", "temporal", "created_at", "dist")
            .dialect(2)
        )
        res = await self.r.ft(self.qa_index).search(q, {"B": _pack(vectors[0])})
        if not res.docs:
            return None, usage
        d = res.docs[0]
        hit = CacheHit(
            key=_s(d.id),
            question=_s(d.question),
            answer=_s(d.answer),
            urls=json.loads(_s(d.urls) or "[]"),
            topic=_s(d.topic),
            temporal=_s(d.temporal),
            created_at=float(_s(d.created_at) or 0),
            similarity=to_similarity(float(_s(d.dist))),
        )
        return hit, usage

    async def search_chunks(self, query: str, k: int) -> tuple[list[ChunkHit], Usage | None]:
        vectors, usage = await self.embedder.embed([normalize_text(query)])
        q = (
            Query(f"(@quarantined:{{0}})=>[KNN {k} @vec $B AS dist]")
            .sort_by("dist")
            .return_fields("text", "url", "title", "section", "fetched_at", "dist")
            .dialect(2)
        )
        res = await self.r.ft(self.chunk_index).search(q, {"B": _pack(vectors[0])})
        hits = [
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
        return hits, usage

    async def chunks_for_url(self, url: str) -> list[ChunkHit]:
        """All clean chunks ingested from one URL, in document order — how a
        recently ingested page is reused instead of re-fetched (ADR-0006).

        Served by the chunk index in one query, never by scanning the keyspace:
        the url TEXT field is matched as an exact phrase, which can over-match
        across token-prefix URLs, so equality is re-checked on the returned rows;
        an under-match (a stopword-heavy URL) yields [], which ingestion treats
        as "nothing reusable" and answers by re-fetching the page.
        `pos` is stored but not indexed (indexes created before it existed would
        silently lack the field), so ordering is client-side over one page's rows."""
        target = normalize_url(url)
        phrase = target.replace("\\", "\\\\").replace('"', '\\"')
        q = (
            Query(f'(@quarantined:{{0}}) (@url:"{phrase}")')
            .return_fields("text", "url", "title", "section", "fetched_at", "pos")
            .paging(0, 128)
            .dialect(2)
        )
        res = await self.r.ft(self.chunk_index).search(q)
        rows = [d for d in res.docs if _s(d.url) == target]
        rows.sort(key=lambda d: float(_s(getattr(d, "pos", "0")) or 0))
        return [
            ChunkHit(
                key=_s(d.id),
                text=_s(d.text),
                url=_s(d.url),
                title=_s(d.title),
                section=_s(d.section),
                fetched_at=float(_s(d.fetched_at) or 0),
                similarity=0.0,
            )
            for d in rows
        ]

    async def iter_cached_questions(self) -> AsyncIterator[tuple[str, list[float]]]:
        """Cached questions with their embedding vectors — the one interface
        analytics clustering consumes; packing and key schema stay in here.

        Cache keys never expire, so vectors from another embedding generation
        (a dimension switch, a test embedder against shared Redis) can coexist;
        incompatible entries are skipped loudly, never crashed on downstream."""
        async for key in self.r.scan_iter(match=f"{self.qa_prefix}*"):
            # redis-py types hybrid sync/async commands as `Awaitable[T] | T`;
            # on the asyncio client every such call is awaitable, hence the ignores.
            data = await self.r.hmget(key, ["question", "vec"])  # ty: ignore[invalid-await]
            if not (data[0] and data[1]):
                continue
            if len(data[1]) % 4 or len(data[1]) // 4 != self.dim:
                log.warning("incompatible_vector_skipped", key=_s(key), nbytes=len(data[1]))
                continue
            yield _s(data[0]), _unpack(data[1])

    # -- writes --------------------------------------------------------------

    async def upsert_chunks(self, records: list[ChunkRecord]) -> tuple[int, Usage | None]:
        """Store chunks idempotently. Quarantined chunks are stored without a
        vector: present for audit, invisible to retrieval (spec §5.5)."""
        clean = [c for c in records if not c.quarantined]
        vectors, usage = (
            await self.embedder.embed([normalize_text(c.text) for c in clean])
            if clean
            else ([], None)
        )
        vec_by_id = {id(c): v for c, v in zip(clean, vectors, strict=True)}
        now = time.time()
        pipe = self.r.pipeline(transaction=False)
        for c in records:
            key = self.chunk_prefix + sha(normalize_text(c.text))
            mapping: dict = {
                "text": c.text,
                "url": normalize_url(c.url),
                "title": c.title,
                "section": c.section,
                "fetched_at": now,
                "quarantined": "1" if c.quarantined else "0",
                "pos": c.pos,
            }
            if id(c) in vec_by_id:
                mapping["vec"] = _pack(vec_by_id[id(c)])
            pipe.hset(key, mapping=mapping)
        await pipe.execute()
        return len(records), usage

    async def put_qa(
        self, question: str, answer: str, urls: list[str], topic: str, temporal: str
    ) -> Usage | None:
        norm = normalize_question(question)
        vectors, usage = await self.embedder.embed([norm])
        await self.r.hset(  # ty: ignore[invalid-await]
            self.qa_prefix + sha(norm),
            mapping={
                "question": question,
                "answer": answer,
                "urls": json.dumps(urls),
                "topic": topic,
                "temporal": temporal,
                "created_at": time.time(),
                "vec": _pack(vectors[0]),
            },
        )
        return usage

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
            if _s(await self.r.hget(key, "url")) == target:  # ty: ignore[invalid-await]
                deleted += await self.r.delete(key)
        async for key in self.r.scan_iter(match=f"{self.qa_prefix}*"):
            raw_urls = await self.r.hget(key, "urls")  # ty: ignore[invalid-await]
            urls = json.loads(_s(raw_urls) or "[]")
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
                raw_ts = await self.r.hget(key, ts_field)  # ty: ignore[invalid-await]
                ts = float(_s(raw_ts) or 0)
                if ts < cutoff:
                    deleted += await self.r.delete(key)
        return deleted

    async def stats(self) -> dict:
        counts = {"chunks": 0, "quarantined": 0, "qa": 0}
        async for key in self.r.scan_iter(match=f"{self.chunk_prefix}*"):
            counts["chunks"] += 1
            if _s(await self.r.hget(key, "quarantined")) == "1":  # ty: ignore[invalid-await]
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
        return bool(await self.r.ping())  # ty: ignore[invalid-await]

    async def aclose(self) -> None:
        await self.r.aclose()
