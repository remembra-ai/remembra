"""Qdrant vector store integration."""

import asyncio
from typing import Any

import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from remembra.config import Settings
from remembra.models.memory import Memory
from remembra.security.encryption import FieldEncryptor

log = structlog.get_logger(__name__)

QDRANT_WRITE_RETRIES = 3
QDRANT_WRITE_BACKOFF_BASE = 0.5  # seconds; doubles each retry

# Payload field names
FIELD_USER_ID = "user_id"
FIELD_PROJECT_ID = "project_id"
FIELD_CONTENT = "content"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"
FIELD_METADATA = "metadata"
# RET-7 / UPG-1: plain (unencrypted) filterable fields
FIELD_MEMORY_TYPE = "memory_type"
FIELD_SCOPE = "scope"
FIELD_SCOPE_PREFIXES = "scope_prefixes"
FIELD_VALID_FROM = "valid_from"
FIELD_VALID_TO = "valid_to"

# Fields ``search(must_match=...)`` may filter on server-side. Anything else
# (notably metadata values, which are encrypted when encryption is on) must
# be filtered by the caller after retrieval.
PLAIN_FILTER_FIELDS = frozenset({FIELD_MEMORY_TYPE, FIELD_SCOPE, FIELD_SCOPE_PREFIXES, FIELD_PROJECT_ID})


def scope_prefixes(scope: str | None) -> list[str]:
    """``"work:acme:q3"`` -> ``["work", "work:acme", "work:acme:q3"]``.

    Stored in the payload so "scope starts with X" is an exact-match array
    filter in Qdrant (a keyword index cannot do prefix matches).
    """
    if not scope:
        return []
    parts = [p for p in str(scope).split(":")]
    return [":".join(parts[: i + 1]) for i in range(len(parts))]


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def filterable_payload(memory: Memory) -> dict[str, Any]:
    """The plain filter fields of a memory (also used for payload-only backfill)."""
    return {
        FIELD_MEMORY_TYPE: memory.memory_type,
        FIELD_SCOPE: memory.scope,
        FIELD_SCOPE_PREFIXES: scope_prefixes(memory.scope),
        FIELD_VALID_FROM: _iso(memory.valid_from or memory.created_at),
        FIELD_VALID_TO: _iso(memory.valid_to),
    }


def _vector_size(collection_info: Any) -> int | None:
    """Vector size of a collection (single unnamed vector, or the first named one)."""
    try:
        vectors = collection_info.config.params.vectors
    except AttributeError:
        return None
    if isinstance(vectors, dict):
        vectors = next(iter(vectors.values()), None)
    size = getattr(vectors, "size", None)
    return int(size) if size is not None else None


class QdrantStore:
    """
    Async Qdrant client wrapper for memory vector storage.

    Handles:
    - Collection initialization with proper schema
    - Upsert/delete operations
    - Semantic search with filtering
    - Transparent AES-256-GCM encryption of content fields
    """

    def __init__(self, settings: Settings, encryptor: FieldEncryptor | None = None) -> None:
        self.settings = settings
        self.collection_name = settings.qdrant_collection
        self._client: AsyncQdrantClient | None = None
        self._encryptor = encryptor or FieldEncryptor(settings.encryption_key)
        self.dimension_status: dict[str, Any] | None = None

    async def _get_client(self) -> AsyncQdrantClient:
        if self._client is None:
            self._client = AsyncQdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key,
                timeout=30,
                prefer_grpc=True,  # gRPC is faster than HTTP for high throughput
            )
        return self._client

    async def init_collection(self, expected_dimensions: int | None = None) -> None:
        """
        Ensure the memories collection exists with correct configuration.
        Safe to call multiple times (idempotent).

        For an existing collection, verifies its vector size matches the
        configured embedding dimensions (REL-6). A mismatch makes every upsert
        and search fail, so it is logged as critical and surfaced via
        ``dimension_status`` (reported by ``/health/ready``) — boot continues
        so the process doesn't crash-loop and keyword paths keep working.
        """
        client = await self._get_client()
        expected = expected_dimensions or self.settings.embedding_dimensions

        if await client.collection_exists(self.collection_name):
            collection_info = await client.get_collection(self.collection_name)
            actual = _vector_size(collection_info)
            self.dimension_status = {
                "collection": self.collection_name,
                "expected": expected,
                "actual": actual,
                "ok": actual is None or expected is None or actual == expected,
            }
            if not self.dimension_status["ok"]:
                log.critical(
                    "qdrant_collection_dimension_mismatch",
                    name=self.collection_name,
                    collection_dimensions=actual,
                    embedding_dimensions=expected,
                    fix="set REMEMBRA_EMBEDDING_DIMENSIONS/MODEL to match, or rebuild via the reindex job",
                )
            log.info(
                "qdrant_collection_exists",
                name=self.collection_name,
                points_count=getattr(collection_info, "points_count", "unknown"),
                dimensions=actual,
            )
            # Indexes added after the collection was created (RET-7 memory_type /
            # scope_prefixes) must exist on old collections too. Idempotent.
            try:
                await self._create_indexes(client)
            except Exception as e:  # noqa: BLE001 - an index is an optimisation, never a boot blocker
                log.warning("qdrant_index_ensure_failed", error=str(e))
            return

        log.info("qdrant_creating_collection", name=self.collection_name, dimensions=expected)
        await self.create_collection(self.collection_name, expected)
        self.dimension_status = {
            "collection": self.collection_name,
            "expected": expected,
            "actual": expected,
            "ok": True,
        }

    async def create_collection(self, name: str, dimensions: int) -> None:
        """Create a cosine collection of ``dimensions`` with Remembra's payload indexes."""
        client = await self._get_client()
        await client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=dimensions,
                distance=qmodels.Distance.COSINE,
            ),
        )
        await self._create_indexes(client, name)

    async def init_collection_with_retry(
        self,
        expected_dimensions: int | None = None,
        attempts: int = 5,
        base_delay: float = 1.0,
    ) -> None:
        """``init_collection`` with exponential backoff (REL-15).

        Qdrant often starts slower than the API container; without retries
        one slow start crash-loops the app. Raises the last error after
        ``attempts`` tries.
        """
        attempts = max(1, attempts)
        for attempt in range(1, attempts + 1):
            try:
                await self.init_collection(expected_dimensions)
                return
            except Exception as e:
                if attempt == attempts:
                    log.error("qdrant_init_failed", attempts=attempt, error=str(e))
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                log.warning("qdrant_init_retry", attempt=attempt, delay=delay, error=str(e))
                # A failed gRPC channel can stay broken; reconnect next attempt.
                await self.close()
                await asyncio.sleep(delay)

    async def _create_indexes(self, client: AsyncQdrantClient, collection_name: str | None = None) -> None:
        """Create payload field indexes for efficient filtering."""
        index_fields = [
            (FIELD_USER_ID, qmodels.PayloadSchemaType.KEYWORD),
            (FIELD_PROJECT_ID, qmodels.PayloadSchemaType.KEYWORD),
            (FIELD_CREATED_AT, qmodels.PayloadSchemaType.DATETIME),
            (FIELD_EXPIRES_AT, qmodels.PayloadSchemaType.DATETIME),
            (FIELD_MEMORY_TYPE, qmodels.PayloadSchemaType.KEYWORD),
            (FIELD_SCOPE_PREFIXES, qmodels.PayloadSchemaType.KEYWORD),
        ]

        for field_name, field_type in index_fields:
            try:
                await client.create_payload_index(
                    collection_name=collection_name or self.collection_name,
                    field_name=field_name,
                    field_schema=field_type,
                )
                log.debug("qdrant_index_created", field=field_name)
            except UnexpectedResponse:
                # Index might already exist
                pass

    async def ensure_indexes(self) -> None:
        """Create any missing payload indexes on the active collection (idempotent)."""
        client = await self._get_client()
        await self._create_indexes(client)

    def build_payload(self, memory: Memory) -> dict[str, Any]:
        """Full Qdrant payload for a memory (content + metadata encrypted when enabled)."""
        payload: dict[str, Any] = {
            FIELD_USER_ID: memory.user_id,
            FIELD_PROJECT_ID: memory.project_id,
            FIELD_CONTENT: self._encryptor.encrypt(memory.content),
            FIELD_CREATED_AT: memory.created_at.isoformat(),
            FIELD_METADATA: self._encryptor.encrypt_dict(memory.metadata),
            **filterable_payload(memory),
        }
        if memory.expires_at:
            payload[FIELD_EXPIRES_AT] = memory.expires_at.isoformat()
        # Add extracted facts and entity refs to payload for retrieval
        payload["extracted_facts"] = memory.extracted_facts or []
        payload["entities"] = [e.model_dump() for e in (memory.entities or [])]
        return payload

    async def upsert(self, memory: Memory) -> None:
        """
        Insert or update a memory in the vector store.

        Args:
            memory: Memory object with embedding already computed
        """
        if not memory.embedding:
            raise ValueError("Memory must have embedding computed before upserting")

        client = await self._get_client()
        payload = self.build_payload(memory)

        point = qmodels.PointStruct(
            id=memory.id,
            vector=memory.embedding,
            payload=payload,
        )
        for attempt in range(1, QDRANT_WRITE_RETRIES + 1):
            try:
                await client.upsert(
                    collection_name=self.collection_name,
                    points=[point],
                )
                break
            except Exception:
                if attempt == QDRANT_WRITE_RETRIES:
                    log.error("qdrant_upsert_failed", memory_id=memory.id, attempts=attempt)
                    raise
                backoff = QDRANT_WRITE_BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning("qdrant_upsert_retry", memory_id=memory.id, attempt=attempt, backoff=backoff)
                await asyncio.sleep(backoff)

        log.debug("qdrant_upserted", memory_id=memory.id, user_id=memory.user_id)

    async def upsert_batch(self, memories: list[Memory]) -> int:
        """
        Bulk insert/update multiple memories in one call.

        Args:
            memories: List of Memory objects with embeddings already computed

        Returns:
            Number of memories upserted
        """
        if not memories:
            return 0

        client = await self._get_client()

        points = []
        for memory in memories:
            if not memory.embedding:
                log.warning("bulk_skip_no_embedding", memory_id=memory.id)
                continue

            payload = self.build_payload(memory)

            points.append(
                qmodels.PointStruct(
                    id=memory.id,
                    vector=memory.embedding,
                    payload=payload,
                )
            )

        if points:
            await client.upsert(
                collection_name=self.collection_name,
                points=points,
            )

        log.info("qdrant_bulk_upserted", count=len(points))
        return len(points)

    async def upsert_vector(self, memory_id: str, vector: list[float]) -> None:
        """Update only the vector for an existing point (used by re-indexer).

        Qdrant's ``update_vectors`` API modifies the vector without
        touching the payload, which is exactly what we want during
        re-embedding.
        """
        client = await self._get_client()
        await client.update_vectors(
            collection_name=self.collection_name,
            points=[
                qmodels.PointVectors(
                    id=memory_id,
                    vector=vector,
                )
            ],
        )

    @property
    def metadata_filterable(self) -> bool:
        """True when metadata values are stored in plaintext (encryption off),
        so ``metadata.<key>`` equality can be pushed into the Qdrant filter."""
        return not self._encryptor.enabled

    @staticmethod
    def _match_values(value: Any) -> list[Any]:
        """Equivalent typed forms of a string filter value.

        Recall filters compare ``str(stored) == str(filter)``; Qdrant matches
        typed values, so "5" must also try 5 and "true" must also try True.
        """
        if not isinstance(value, str):
            return [value]
        forms: list[Any] = [value]
        low = value.strip().lower()
        if low in ("true", "false"):
            forms.append(low == "true")
        else:
            try:
                forms.append(int(value))
            except ValueError:
                pass
        return forms

    def build_filter(
        self,
        user_id: str,
        project_id: str | None = None,
        *,
        active_at: Any = None,
        must_match: dict[str, Any] | None = None,
        metadata_match: dict[str, Any] | None = None,
    ) -> qmodels.Filter:
        """Qdrant filter: user (+ project) scope, not expired at ``active_at``,
        exact matches on plain fields, and (plaintext only) metadata equality."""
        must: list[Any] = [qmodels.FieldCondition(key=FIELD_USER_ID, match=qmodels.MatchValue(value=user_id))]
        if project_id is not None:
            must.append(qmodels.FieldCondition(key=FIELD_PROJECT_ID, match=qmodels.MatchValue(value=project_id)))
        for key, value in (must_match or {}).items():
            if key not in PLAIN_FILTER_FIELDS:
                raise ValueError(f"{key!r} is not a plain payload field")
            if isinstance(value, list | tuple | set):
                must.append(qmodels.FieldCondition(key=key, match=qmodels.MatchAny(any=[str(v) for v in value])))
            else:
                must.append(qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value)))
        if metadata_match and self.metadata_filterable:
            for key, value in metadata_match.items():
                forms = self._match_values(value)
                conds = [qmodels.FieldCondition(key=f"{FIELD_METADATA}.{key}", match=qmodels.MatchValue(value=f)) for f in forms]
                must.append(conds[0] if len(conds) == 1 else qmodels.Filter(should=conds))
        if active_at is not None:
            # RET-2: never return a memory whose expiry has passed.
            must.append(
                qmodels.Filter(
                    should=[
                        qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key=FIELD_EXPIRES_AT)),
                        qmodels.IsNullCondition(is_null=qmodels.PayloadField(key=FIELD_EXPIRES_AT)),
                        qmodels.FieldCondition(key=FIELD_EXPIRES_AT, range=qmodels.DatetimeRange(gt=active_at)),
                    ]
                )
            )
        return qmodels.Filter(must=must)

    async def search(
        self,
        query_vector: list[float],
        user_id: str,
        project_id: str | None = None,
        limit: int = 5,
        score_threshold: float = 0.70,
        *,
        offset: int = 0,
        active_at: Any = None,
        must_match: dict[str, Any] | None = None,
        metadata_match: dict[str, Any] | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """
        Semantic search for memories.

        Args:
            query_vector: Embedding of the search query
            user_id: Filter to this user's memories (always required)
            project_id: Filter to this project. If None, search across
                all projects owned by the user (cross-project recall).
            limit: Max results to return
            score_threshold: Minimum cosine similarity
            offset: Skip this many best hits (paging for over-fetch loops)
            active_at: Exclude memories whose ``expires_at`` <= this time
            must_match: Exact match on plain payload fields (``PLAIN_FILTER_FIELDS``)
            metadata_match: ``metadata.<key>`` equality; applied server-side only
                when metadata is unencrypted — callers must still verify.

        Returns:
            List of (memory_id, score, payload) tuples
        """
        client = await self._get_client()

        results = await client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=self.build_filter(
                user_id, project_id, active_at=active_at, must_match=must_match, metadata_match=metadata_match
            ),
            limit=limit,
            offset=offset or None,
            score_threshold=score_threshold,
        )

        return [(str(r.id), r.score, self._decrypt_payload(r.payload or {})) for r in results.points]

    async def score_ids(self, query_vector: list[float], memory_ids: list[str], user_id: str | None = None) -> dict[str, float]:
        """Cosine similarity of ``query_vector`` to specific points (RET-5).

        One filtered query instead of fetching each vector. Points owned by
        another user are never scored when ``user_id`` is given.
        """
        if not memory_ids:
            return {}
        client = await self._get_client()
        must: list[Any] = [qmodels.HasIdCondition(has_id=list(memory_ids))]
        if user_id is not None:
            must.append(qmodels.FieldCondition(key=FIELD_USER_ID, match=qmodels.MatchValue(value=user_id)))
        results = await client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=qmodels.Filter(must=must),
            limit=len(memory_ids),
            with_payload=False,
        )
        return {str(r.id): float(r.score) for r in results.points}

    async def set_payload_fields(self, memory_id: str, fields: dict[str, Any]) -> None:
        """Overwrite only the given payload keys of one point (backfills)."""
        client = await self._get_client()
        await client.set_payload(collection_name=self.collection_name, payload=fields, points=[memory_id])

    async def get_raw_payloads(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Stored payloads (not decrypted) for the given ids; missing ids omitted."""
        if not memory_ids:
            return {}
        client = await self._get_client()
        points = await client.retrieve(
            collection_name=self.collection_name, ids=list(memory_ids), with_payload=True, with_vectors=False
        )
        return {str(p.id): dict(p.payload or {}) for p in points}

    async def set_metadata(self, memory_id: str, metadata: dict[str, Any]) -> None:
        """Replace a point's metadata payload (encrypted like on upsert)."""
        await self.set_payload_fields(memory_id, {FIELD_METADATA: self._encryptor.encrypt_dict(metadata)})

    async def existing_ids(self, memory_ids: list[str]) -> set[str]:
        """Which of ``memory_ids`` have a point in the collection."""
        if not memory_ids:
            return set()
        client = await self._get_client()
        points = await client.retrieve(
            collection_name=self.collection_name, ids=list(memory_ids), with_payload=False, with_vectors=False
        )
        return {str(p.id) for p in points}

    async def delete(self, memory_id: str, user_id: str | None = None) -> bool:
        """Delete a single memory by ID.

        With ``user_id`` the delete is filtered to that owner, so a point owned
        by another user is never removed (ING-2).
        """
        client = await self._get_client()

        selector: qmodels.PointIdsList | qmodels.FilterSelector
        if user_id is None:
            selector = qmodels.PointIdsList(points=[memory_id])
        else:
            selector = qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.HasIdCondition(has_id=[memory_id]),
                        qmodels.FieldCondition(key=FIELD_USER_ID, match=qmodels.MatchValue(value=user_id)),
                    ]
                )
            )
        result = await client.delete(
            collection_name=self.collection_name,
            points_selector=selector,
        )

        log.debug("qdrant_deleted", memory_id=memory_id, status=result.status)
        return result.status == qmodels.UpdateStatus.COMPLETED

    async def delete_by_user(self, user_id: str) -> int:
        """Delete all memories for a user. Returns count deleted."""
        client = await self._get_client()

        # First count how many we're deleting
        count_result = await client.count(
            collection_name=self.collection_name,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key=FIELD_USER_ID,
                        match=qmodels.MatchValue(value=user_id),
                    )
                ]
            ),
        )

        # Delete by filter
        await client.delete(
            collection_name=self.collection_name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key=FIELD_USER_ID,
                            match=qmodels.MatchValue(value=user_id),
                        )
                    ]
                )
            ),
        )

        log.info("qdrant_deleted_user_memories", user_id=user_id, count=count_result.count)
        return count_result.count

    async def delete_by_project(self, user_id: str, project_id: str) -> int:
        """Delete all memories for a user within a specific project. Returns count deleted.

        SECURITY: Always requires user_id to prevent cross-user deletion.
        """
        client = await self._get_client()

        # First count how many we're deleting
        count_result = await client.count(
            collection_name=self.collection_name,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key=FIELD_USER_ID,
                        match=qmodels.MatchValue(value=user_id),
                    ),
                    qmodels.FieldCondition(
                        key=FIELD_PROJECT_ID,
                        match=qmodels.MatchValue(value=project_id),
                    ),
                ]
            ),
        )

        # Delete by filter
        await client.delete(
            collection_name=self.collection_name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key=FIELD_USER_ID,
                            match=qmodels.MatchValue(value=user_id),
                        ),
                        qmodels.FieldCondition(
                            key=FIELD_PROJECT_ID,
                            match=qmodels.MatchValue(value=project_id),
                        ),
                    ]
                )
            ),
        )

        log.info("qdrant_deleted_project_memories", user_id=user_id, project_id=project_id, count=count_result.count)
        return count_result.count

    async def get_by_id(self, memory_id: str) -> dict[str, Any] | None:
        """Retrieve a memory by ID."""
        client = await self._get_client()

        results = await client.retrieve(
            collection_name=self.collection_name,
            ids=[memory_id],
            with_payload=True,
            with_vectors=False,
        )

        if not results:
            return None

        point = results[0]
        if point.payload:
            return {"id": point.id, **self._decrypt_payload(point.payload)}
        return {"id": point.id}

    def _decrypt_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Decrypt content and metadata fields in a Qdrant payload."""
        result = dict(payload)
        if FIELD_CONTENT in result and isinstance(result[FIELD_CONTENT], str):
            result[FIELD_CONTENT] = self._encryptor.decrypt(result[FIELD_CONTENT])
        if FIELD_METADATA in result and isinstance(result[FIELD_METADATA], dict):
            result[FIELD_METADATA] = self._encryptor.decrypt_dict(result[FIELD_METADATA])
        return result

    async def health_check(self, timeout: float = 3.0) -> bool:
        """Check Qdrant over the transport the app actually uses (gRPC when
        ``prefer_grpc``) — an HTTP ``/healthz`` can pass while gRPC is broken (REL-19)."""
        try:
            client = await self._get_client()
            await asyncio.wait_for(client.get_collections(), timeout=timeout)
            return True
        except Exception as e:
            log.warning("qdrant_health_check_failed", error=str(e))
            return False

    async def close(self) -> None:
        """Close the client connection."""
        if self._client:
            await self._client.close()
            self._client = None
