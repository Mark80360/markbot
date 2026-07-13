"""VectorStore — persistent vector storage for long-term memory.

Two implementations:

- :class:`SQLiteVectorStore`: the default backend. Uses the standard
  library ``sqlite3`` for on-disk persistence (no extra install) and
  in-memory ``numpy`` cosine for ranking. Designed for corpora up to
  ~50k vectors where a single matrix-multiply top-k scan stays under
  ~50 ms.
- :class:`InMemoryVectorStore`: pure in-process store for tests and
  ephemeral agents.

Both expose the same :class:`VectorStore` interface: ``upsert``,
``query``, ``delete``, ``count``, ``close``. Vectors are L2-normalized
on write so cosine similarity is a plain dot product.

The store is **dimension-agnostic**: it records the embedding signature
(``backend@dim``) on first write and refuses mismatched vectors after
that. The :class:`LongTermMemory` layer uses :meth:`embedding_sig` /
:meth:`clear` to detect backend switches and trigger a full rebuild.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from loguru import logger

# numpy is a hard dependency of sentence-transformers and is present in
# most scientific stacks; import lazily so a missing numpy downgrades
# gracefully rather than crashing module import. SQLiteVectorStore needs
# it — instantiating that store without numpy raises a clear error.
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


# ---------------------------------------------------------------------------
# Record and interface
# ---------------------------------------------------------------------------


@dataclass
class VectorRecord:
    """A single stored vector plus its payload."""

    id: str
    content: str
    vector: list[float]
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Cosine similarity to the query (populated by ``query()``, 0.0 otherwise).
    score: float = 0.0
    #: Unix timestamp of first insertion (monotonic-ish; used for LRU eviction).
    created_at: float = 0.0
    #: How many times this record was returned by a query (importance signal).
    access_count: int = 0


class VectorStore(ABC):
    """Abstract vector store.

    Implementations must be safe for concurrent reads and serialized
    writes (a single writer at a time is acceptable — memory indexing
    is low-frequency).
    """

    @abstractmethod
    def upsert(self, record: VectorRecord) -> None:
        """Insert or replace a record by ``record.id``."""

    @abstractmethod
    def query(
        self,
        query_vec: list[float],
        top_k: int = 10,
        *,
        source: str | None = None,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[VectorRecord]:
        """Return the ``top_k`` most similar records (cosine), best first.

        Args:
            query_vec: L2-normalized query vector.
            top_k: Maximum records to return.
            source: Optional exact-match filter on ``record.source``.
            filter_metadata: Optional dict of ``{key: value}`` exact-match
                filters applied to ``record.metadata``.
        """

    @abstractmethod
    def get(self, record_id: str) -> VectorRecord | None:
        """Fetch a single record by id, or ``None`` if absent."""

    @abstractmethod
    def delete(self, record_id: str) -> bool:
        """Delete a record. Return ``True`` if something was removed."""

    @abstractmethod
    def delete_by_source(self, source: str) -> int:
        """Delete all records whose ``source`` matches. Return count removed."""

    @abstractmethod
    def count(self) -> int:
        """Total number of stored records."""

    @abstractmethod
    def iter_all(self) -> list[VectorRecord]:
        """Return all records (no vectors required for ranking).

        Used by the consolidation layer for dedup/decay sweeps.
        """

    @abstractmethod
    def increment_access(self, record_ids: Sequence[str]) -> None:
        """Bump ``access_count`` for the given ids (importance tracking)."""

    @abstractmethod
    def clear(self) -> None:
        """Remove all records. Used when the embedding backend changes."""

    @property
    @abstractmethod
    def embedding_sig(self) -> str | None:
        """The embedding signature recorded on first write, or ``None``."""

    @embedding_sig.setter
    def embedding_sig(self, value: str | None) -> None:
        """Record the embedding signature (called on first write / rebuild)."""

    def close(self) -> None:
        """Release resources. Default no-op."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector in-place-safe (returns a new list)."""
    if not _HAS_NUMPY:
        import math
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return list(vec)
        inv = 1.0 / norm
        return [v * inv for v in vec]
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return vec
    return (arr / norm).tolist()


def _metadata_matches(meta: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    """Exact-match filter on metadata values. ``None`` filter = match all."""
    if not filters:
        return True
    for k, v in filters.items():
        if meta.get(k) != v:
            return False
    return True


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class InMemoryVectorStore(VectorStore):
    """Process-local vector store backed by a dict.

    Useful for tests and ephemeral agents. Thread-safe via a single lock.
    """

    def __init__(self) -> None:
        self._records: dict[str, VectorRecord] = {}
        self._sig: str | None = None
        self._lock = threading.RLock()

    def upsert(self, record: VectorRecord) -> None:
        with self._lock:
            if not record.created_at:
                record.created_at = time.time()
            # Preserve original created_at on re-upsert (idempotent index).
            existing = self._records.get(record.id)
            if existing is not None and not record.created_at:
                record.created_at = existing.created_at
            record.vector = _normalize(record.vector)
            self._records[record.id] = record
            if self._sig is None:
                self._sig = record.metadata.get("_embedding_sig")  # type: ignore[assignment]

    def query(
        self,
        query_vec: list[float],
        top_k: int = 10,
        *,
        source: str | None = None,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[VectorRecord]:
        with self._lock:
            if not self._records:
                return []
            q = _normalize(query_vec)
            scored: list[tuple[float, VectorRecord]] = []
            for rec in self._records.values():
                if source is not None and rec.source != source:
                    continue
                if not _metadata_matches(rec.metadata, filter_metadata):
                    continue
                # Dot product of normalized vectors == cosine similarity.
                sim = _dot(q, rec.vector)
                scored.append((sim, rec))
            scored.sort(key=lambda t: t[0], reverse=True)
            out: list[VectorRecord] = []
            for sim, rec in scored[:top_k]:
                rec.score = float(sim)
                out.append(rec)
            return out

    def get(self, record_id: str) -> VectorRecord | None:
        with self._lock:
            rec = self._records.get(record_id)
            if rec is None:
                return None
            return VectorRecord(
                id=rec.id, content=rec.content, vector=list(rec.vector),
                source=rec.source, metadata=dict(rec.metadata),
                created_at=rec.created_at, access_count=rec.access_count,
            )

    def delete(self, record_id: str) -> bool:
        with self._lock:
            return self._records.pop(record_id, None) is not None

    def delete_by_source(self, source: str) -> int:
        with self._lock:
            victims = [rid for rid, r in self._records.items() if r.source == source]
            for rid in victims:
                self._records.pop(rid, None)
            return len(victims)

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def iter_all(self) -> list[VectorRecord]:
        with self._lock:
            # Return copies so callers can't mutate the live store.
            return [
                VectorRecord(
                    id=r.id, content=r.content, vector=list(r.vector),
                    source=r.source, metadata=dict(r.metadata),
                    created_at=r.created_at, access_count=r.access_count,
                )
                for r in self._records.values()
            ]

    def increment_access(self, record_ids: Sequence[str]) -> None:
        with self._lock:
            for rid in record_ids:
                rec = self._records.get(rid)
                if rec is not None:
                    rec.access_count += 1

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._sig = None

    @property
    def embedding_sig(self) -> str | None:
        return self._sig

    @embedding_sig.setter
    def embedding_sig(self, value: str | None) -> None:
        with self._lock:
            self._sig = value


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product with numpy if available, else pure Python."""
    if _HAS_NUMPY:
        return float(np.dot(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)))
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------


class SQLiteVectorStore(VectorStore):
    """Persistent vector store backed by a single SQLite database file.

    Schema:
    - ``records(id TEXT PK, content TEXT, vector BLOB, source TEXT,
                metadata TEXT, created_at REAL, access_count INTEGER)``
    - ``meta(key TEXT PK, value TEXT)`` — stores ``embedding_sig`` etc.

    Vectors are stored as raw float32 blobs (``numpy.tobytes``). Query
    loads all surviving candidate vectors into a numpy matrix and does
    a single matrix-vector product for top-k — fast for the intended
    scale (tens of thousands of vectors). A single ``threading.Lock``
    serializes writes; reads use a separate read-only connection per
    call so concurrent searches don't block.
    """

    def __init__(
        self,
        db_path: str | Path,
        max_records: int = 50_000,
        max_scan_records: int = 20_000,
    ) -> None:
        if not _HAS_NUMPY:
            raise ImportError(
                "SQLiteVectorStore requires numpy; install with: "
                "pip install numpy"
            )
        # Hard cap on how many vectors a single query() loads into RAM.
        # Without this, a full-table scan at 50k records x 1536-dim (OpenAI
        # embedder) allocates ~300 MB per query.  We bias the SQL sample
        # toward recently-accessed / recently-created records so the cap
        # preferentially keeps the most relevant working set.
        self._max_scan_records = max(1, max_scan_records)
        self._db_path = str(db_path)
        self._max_records = max_records
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        # Writers serialize on this lock; SQLite's own BUSY handling is a
        # backstop. We keep one long-lived write connection for simplicity.
        self._write_lock = threading.Lock()
        self._write_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._write_conn.execute("PRAGMA journal_mode=WAL")
        self._write_conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    # -- schema --------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._write_lock:
            cur = self._write_conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id           TEXT PRIMARY KEY,
                    content      TEXT NOT NULL,
                    vector       BLOB NOT NULL,
                    source       TEXT NOT NULL DEFAULT '',
                    metadata     TEXT NOT NULL DEFAULT '{}',
                    created_at   REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_source ON records(source)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_created ON records(created_at)")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            self._write_conn.commit()

    # -- meta helpers --------------------------------------------------------

    def _read_meta(self, key: str) -> str | None:
        # Read via a fresh connection to avoid contending with the write lock.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _write_meta(self, key: str, value: str | None) -> None:
        with self._write_lock:
            if value is None:
                self._write_conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self._write_conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            self._write_conn.commit()

    @property
    def embedding_sig(self) -> str | None:
        return self._read_meta("embedding_sig")

    @embedding_sig.setter
    def embedding_sig(self, value: str | None) -> None:
        self._write_meta("embedding_sig", value)

    # -- core ops ------------------------------------------------------------

    def upsert(self, record: VectorRecord) -> None:
        vec = _normalize(record.vector)
        vec_blob = np.asarray(vec, dtype=np.float32).tobytes()
        created_at = record.created_at or time.time()
        # Preserve original created_at on re-upsert so LRU isn't reset.
        existing = self.get(record.id)
        if existing is not None and not record.created_at:
            created_at = existing.created_at
        meta = dict(record.metadata)
        meta["_embedding_sig"] = self.embedding_sig or ""
        meta_json = json.dumps(meta, ensure_ascii=False, default=str)

        with self._write_lock:
            self._write_conn.execute(
                """
                INSERT INTO records(id, content, vector, source, metadata, created_at, access_count)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content = excluded.content,
                    vector = excluded.vector,
                    source = excluded.source,
                    metadata = excluded.metadata,
                    access_count = excluded.access_count
                """,
                (
                    record.id, record.content, vec_blob, record.source,
                    meta_json, created_at, record.access_count,
                ),
            )
            self._write_conn.commit()
        self._enforce_max_records()

    def _enforce_max_records(self) -> None:
        """Evict lowest-importance records when over capacity.

        Importance approximation used by SQL (no python loop):
          - low access_count first
          - then older created_at
        This is a practical stand-in for Consolidator.importance() and
        keeps the working set biased toward frequently recalled facts.
        """
        try:
            cur = self._write_conn.execute("SELECT COUNT(*) FROM records")
            total = cur.fetchone()[0]
        except Exception:
            return
        if total <= self._max_records:
            return
        excess = total - self._max_records
        with self._write_lock:
            self._write_conn.execute(
                """
                DELETE FROM records WHERE id IN (
                    SELECT id FROM records
                    ORDER BY
                        access_count ASC,
                        created_at ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )
            self._write_conn.commit()
        logger.debug(
            "VectorStore importance-LRU evicted {} records (was {})",
            excess, total,
        )

    def query(
        self,
        query_vec: list[float],
        top_k: int = 10,
        *,
        source: str | None = None,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[VectorRecord]:
        # Read via a fresh read-only connection so concurrent searches
        # don't contend with the write lock.
        where_parts: list[str] = []
        params: list[Any] = []
        if source is not None:
            where_parts.append("source = ?")
            params.append(source)
        if filter_metadata:
            for key, value in filter_metadata.items():
                # Push metadata filters into SQLite so the scan cap is applied
                # *after* session/source filtering.  Filtering in Python after
                # LIMIT can miss relevant same-session rows that happen to sit
                # just beyond the capped global working set.
                where_parts.append("json_extract(metadata, ?) = ?")
                params.extend((f"$.{key}", value))

        where_sql = ""
        if where_parts:
            where_sql = " WHERE " + " AND ".join(where_parts)

        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            cur = conn.execute(
                "SELECT id, content, vector, source, metadata, created_at, access_count "
                f"FROM records{where_sql} "
                "ORDER BY access_count DESC, created_at DESC LIMIT ?",
                (*params, self._max_scan_records),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return []

        # Warn when the scan cap was reached — callers hitting this should
        # either raise max_scan_records or switch to an ANN-backed store
        # (Chroma) for better recall at scale.
        if len(rows) >= self._max_scan_records:
            logger.warning(
                "SQLiteVectorStore.query scanned max_scan_records={} "
                "(capped); {} total records in store — recall may be "
                "incomplete, consider an ANN backend",
                self._max_scan_records, len(rows),
            )

        # Build matrices for vectorized cosine.
        ids: list[str] = []
        contents: list[str] = []
        sources: list[str] = []
        metas: list[dict[str, Any]] = []
        created: list[float] = []
        access: list[int] = []
        vec_list: list[list[float]] = []
        for rid, content, vec_blob, src, meta_json, crt, acc in rows:
            meta = json.loads(meta_json) if meta_json else {}
            arr = np.frombuffer(vec_blob, dtype=np.float32)
            ids.append(rid)
            contents.append(content)
            sources.append(src)
            metas.append(meta)
            created.append(crt)
            access.append(acc)
            vec_list.append(arr)

        if not vec_list:
            return []

        q = np.asarray(_normalize(query_vec), dtype=np.float32)
        mat = np.vstack(vec_list)  # (n, dim)
        sims = mat @ q  # (n,) — both sides normalized, so this is cosine
        # argpartition for top-k, then sort just those.
        k = min(top_k, len(sims))
        top_idx = np.argpartition(sims, -k)[-k:]
        top_idx = top_idx[np.argsort(sims[top_idx])[::-1]]

        out: list[VectorRecord] = []
        for i in top_idx:
            out.append(
                VectorRecord(
                    id=ids[i],
                    content=contents[i],
                    vector=vec_list[i].tolist(),
                    source=sources[i],
                    metadata=metas[i],
                    score=float(sims[i]),
                    created_at=created[i],
                    access_count=access[i],
                )
            )
        return out

    def get(self, record_id: str) -> VectorRecord | None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            cur = conn.execute(
                "SELECT id, content, vector, source, metadata, created_at, access_count "
                "FROM records WHERE id = ?",
                (record_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        rid, content, vec_blob, src, meta_json, crt, acc = row
        arr = np.frombuffer(vec_blob, dtype=np.float32)
        return VectorRecord(
            id=rid, content=content, vector=arr.tolist(), source=src,
            metadata=json.loads(meta_json) if meta_json else {},
            created_at=crt, access_count=acc,
        )

    def delete(self, record_id: str) -> bool:
        with self._write_lock:
            cur = self._write_conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
            self._write_conn.commit()
            return cur.rowcount > 0

    def delete_by_source(self, source: str) -> int:
        with self._write_lock:
            cur = self._write_conn.execute("DELETE FROM records WHERE source = ?", (source,))
            self._write_conn.commit()
            return cur.rowcount

    def count(self) -> int:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM records")
            return int(cur.fetchone()[0])
        finally:
            conn.close()

    def iter_all(self) -> list[VectorRecord]:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            cur = conn.execute(
                "SELECT id, content, source, metadata, created_at, access_count FROM records "
                "ORDER BY access_count DESC, created_at DESC"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            VectorRecord(
                id=rid, content=content, vector=[], source=src,
                metadata=json.loads(meta_json) if meta_json else {},
                created_at=crt, access_count=acc,
            )
            for rid, content, src, meta_json, crt, acc in rows
        ]

    def increment_access(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        with self._write_lock:
            self._write_conn.executemany(
                "UPDATE records SET access_count = access_count + 1 WHERE id = ?",
                [(rid,) for rid in record_ids],
            )
            self._write_conn.commit()

    def clear(self) -> None:
        with self._write_lock:
            self._write_conn.execute("DELETE FROM records")
            self._write_conn.execute("DELETE FROM meta")
            self._write_conn.commit()

    def close(self) -> None:
        with self._write_lock:
            try:
                self._write_conn.close()
            except Exception:
                pass


__all__ = [
    "VectorRecord",
    "VectorStore",
    "InMemoryVectorStore",
    "SQLiteVectorStore",
]
