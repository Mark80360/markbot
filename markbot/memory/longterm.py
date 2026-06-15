"""LongTermMemory — the semantic-recall facade over embedder + vectorstore.

This is the layer that gives ``MemoryManager.memory_search()`` real
semantic recall. It:

- **indexes** content (turns, memory writes, delegations) into the
  vector store as it arrives, deduplicating by content hash,
- **searches** by encoding the query and retrieving the top-k most
  similar stored vectors, then applies min-score and session filters,
- **reindexes** when the embedding backend changes (detected via
  ``embedding_sig`` mismatch) so a backend switch doesn't mix
  incompatible vectors,
- exposes a **keyword fallback** (delegated to the caller) so the
  manager can fuse keyword + vector results via RRF.

Design notes
------------
- Indexing is **synchronous** but cheap for the hashing/ST backends;
  for the OpenAI backend the caller should run it on a thread executor
  (``MemoryManager`` already offloads sync_turn work via a thread pool).
- Search is **synchronous** and fast (<50 ms for tens of thousands of
  vectors). The manager wraps it in ``run_in_executor`` to stay async.
- IDs are deterministic ``sha256(content)`` so re-indexing the same
  content is idempotent and upserts rather than duplicates.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from loguru import logger

from .embedder import Embedder, build_embedder
from .vectorstore import VectorRecord, VectorStore

#: Drop content shorter than this — it adds noise without signal.
#: Kept low (12) so concise Chinese phrases (which are denser per char
#: than English) still get indexed; English boilerplate like "ok" or
#: "yes" stays filtered out.
DEFAULT_MIN_CONTENT_CHARS = 12


@dataclass
class LongTermConfig:
    """Tunables for :class:`LongTermMemory`."""

    min_content_chars: int = DEFAULT_MIN_CONTENT_CHARS
    #: Over-fetch factor: retrieve ``max_results * multiplier`` from the
    #: vector store, then let the manager's RRF fusion trim down.
    top_k_multiplier: int = 2
    #: Number of worker threads for the index executor. 1 is enough —
    #: indexing is low-frequency and writes serialize at the store.
    index_workers: int = 1


class LongTermMemory:
    """Semantic long-term memory: embed → store → recall.

    Args:
        working_dir: Workspace root (used for the default SQLite path).
        embedder: The :class:`Embedder` to use. If ``None``, one is
            built from ``embedding_config`` via :func:`build_embedder`.
        vectorstore: The :class:`VectorStore` to use. If ``None``, the
            caller must supply one (the factory in
            ``vectorstore_factory`` builds the default SQLite store).
        embedding_config: Config dict forwarded to ``build_embedder``
            when ``embedder`` is not provided.
        config: :class:`LongTermConfig` tunables.
    """

    def __init__(
        self,
        working_dir: str | Path,
        *,
        embedder: Embedder | None = None,
        vectorstore: VectorStore | None = None,
        embedding_config: dict[str, Any] | None = None,
        config: LongTermConfig | None = None,
    ) -> None:
        self.working_dir = Path(working_dir)
        self.config = config or LongTermConfig()
        self._embedder = embedder or build_embedder(embedding_config)
        if vectorstore is None:
            raise ValueError(
                "LongTermMemory requires a vectorstore; pass one or use "
                "the factory (markbot.memory.vectorstore_factory.build_vectorstore)"
            )
        self._store = vectorstore
        # Single-worker executor so indexing never blocks the event loop
        # and concurrent index calls are ordered.
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.index_workers,
            thread_name_prefix="ltm-index",
        )
        # Detect a backend switch on startup.
        self._reconcile_sig()

    # -- properties ----------------------------------------------------------

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def store(self) -> VectorStore:
        return self._store

    @property
    def count(self) -> int:
        return self._store.count()

    # -- embedding-signature reconciliation ----------------------------------

    def _reconcile_sig(self) -> None:
        """If the stored embedding signature doesn't match the current
        embedder, clear the store so subsequent writes use the new space.

        This makes backend switches safe: switching from hashing →
        sentence-transformers wipes the low-quality hashed vectors and
        rebuilds from source (the manager triggers ``reindex_all``).
        """
        stored = self._store.embedding_sig
        current = self._embedder.embedding_sig
        if stored is None:
            # First use — record our signature.
            self._store.embedding_sig = current
            logger.info("LongTermMemory: initialized (sig={}, store empty)", current)
        elif stored != current:
            logger.warning(
                "LongTermMemory: embedding backend changed ({} → {}); "
                "clearing {} stale vectors for rebuild",
                stored, current, self._store.count(),
            )
            self._store.clear()
            self._store.embedding_sig = current
        else:
            logger.debug(
                "LongTermMemory: ready (sig={}, {} records)",
                current, self._store.count(),
            )

    # -- content id ----------------------------------------------------------

    @staticmethod
    def _content_id(content: str) -> str:
        """Deterministic record id from content (sha256, truncated).

        Same content → same id → upsert (no duplicates). The id is
        also used as a dedup key by the consolidation layer.
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]

    # -- indexing ------------------------------------------------------------

    def index(
        self,
        content: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Embed and store one piece of content.

        Short / empty content is skipped silently. Returns the record
        id on success, or ``None`` if the content was filtered out.

        This method is **synchronous** and may block on embedding
        (notably for the OpenAI backend). Call it from a thread for
        non-blocking use.
        """
        if not content or not isinstance(content, str):
            return None
        content = content.strip()
        if len(content) < self.config.min_content_chars:
            return None

        try:
            vec = self._embedder.embed_one(content)
        except Exception as exc:
            logger.debug("LongTermMemory.index embed failed: {}", exc)
            return None

        meta = dict(metadata or {})
        meta.setdefault("first_seen", time.time())
        record = VectorRecord(
            id=self._content_id(content),
            content=content,
            vector=vec,
            source=source,
            metadata=meta,
        )
        try:
            self._store.upsert(record)
            return record.id
        except Exception as exc:
            logger.debug("LongTermMemory.index upsert failed: {}", exc)
            return None

    def index_many(
        self,
        items: Sequence[tuple[str, str, dict[str, Any] | None]],
    ) -> int:
        """Batch-index ``(content, source, metadata)`` tuples.

        Batches the embedding call (the dominant cost) and then upserts
        each record. Returns the number actually stored.
        """
        # Filter valid items first.
        valid: list[tuple[int, str, str, dict[str, Any] | None]] = []
        for idx, (content, source, metadata) in enumerate(items):
            if not content or not isinstance(content, str):
                continue
            content = content.strip()
            if len(content) < self.config.min_content_chars:
                continue
            valid.append((idx, content, source, metadata))
        if not valid:
            return 0

        try:
            vecs = self._embedder.embed([c for _, c, _, _ in valid])
        except Exception as exc:
            logger.warning("LongTermMemory.index_many embed failed: {}", exc)
            return 0

        stored = 0
        now = time.time()
        for (idx, content, source, metadata), vec in zip(valid, vecs):
            meta = dict(metadata or {})
            meta.setdefault("first_seen", now)
            try:
                self._store.upsert(VectorRecord(
                    id=self._content_id(content),
                    content=content,
                    vector=vec,
                    source=source,
                    metadata=meta,
                ))
                stored += 1
            except Exception as exc:
                logger.debug("LongTermMemory.index_many upsert failed: {}", exc)
        return stored

    def index_async(
        self,
        content: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Fire-and-forget index on the background executor.

        Safe to call from async code without awaiting. Exceptions are
        logged and swallowed — indexing must never break a turn.
        """
        def _task() -> None:
            try:
                self.index(content, source, metadata)
            except Exception as exc:
                logger.debug("LongTermMemory.index_async failed: {}", exc)

        self._executor.submit(_task)

    # -- search --------------------------------------------------------------

    def search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search returning ``[{content, source, score, ...}]``.

        The return shape matches the existing ``memory_search`` contract
        (``content``/``source``/``score`` keys) so the manager can fuse
        these with keyword results without reshaping.
        """
        if not query or not query.strip():
            return []
        try:
            qvec = self._embedder.embed_one(query)
        except Exception as exc:
            logger.debug("LongTermMemory.search embed failed: {}", exc)
            return []

        top_k = max(max_results * self.config.top_k_multiplier, max_results)

        filter_meta: dict[str, Any] | None = None
        if channel and chat_id:
            # Session-scoped recall: only the same channel+chat_id.
            filter_meta = {"channel": channel, "chat_id": chat_id}
        elif channel:
            filter_meta = {"channel": channel}

        try:
            records = self._store.query(
                qvec, top_k=top_k, source=source, filter_metadata=filter_meta,
            )
        except Exception as exc:
            logger.debug("LongTermMemory.search query failed: {}", exc)
            return []

        # If session-scoped filtering wiped everything but we have no
        # explicit session request, fall back to unscoped recall so the
        # agent still gets *some* relevant memory.
        if not records and filter_meta is not None:
            try:
                records = self._store.query(qvec, top_k=top_k, source=source)
            except Exception:
                pass

        results: list[dict[str, Any]] = []
        hit_ids: list[str] = []
        for rec in records:
            if rec.score < min_score:
                continue
            results.append({
                "content": rec.content[:2000],
                "source": f"vector/{rec.source}",
                "score": round(float(rec.score), 4),
                "id": rec.id,
                "metadata": rec.metadata,
            })
            hit_ids.append(rec.id)

        # Bump access counts for recalled records (importance signal).
        if hit_ids:
            try:
                self._store.increment_access(hit_ids)
            except Exception:
                pass

        return results

    # -- bulk operations -----------------------------------------------------

    def reindex_all(
        self,
        items: Sequence[tuple[str, str, dict[str, Any] | None]],
        batch_size: int = 256,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> int:
        """Rebuild the index from a list of items.

        Clears the store (recording the current embedding signature)
        then re-indexes in batches. Used on first enable and after a
        backend switch. Returns the number of items stored.
        """
        total = len(items)
        if total == 0:
            self._store.clear()
            self._store.embedding_sig = self._embedder.embedding_sig
            return 0
        logger.info("LongTermMemory: reindexing {} items (sig={})", total, self._embedder.embedding_sig)
        self._store.clear()
        self._store.embedding_sig = self._embedder.embedding_sig

        stored = 0
        for start in range(0, total, batch_size):
            batch = items[start : start + batch_size]
            stored += self.index_many(batch)
            if progress_cb:
                try:
                    progress_cb(start + len(batch), total)
                except Exception:
                    pass
        logger.info("LongTermMemory: reindex complete ({} stored)", stored)
        return stored

    def iter_all_sources(self) -> dict[str, int]:
        """Return ``{source: count}`` for diagnostic / consolidation use."""
        counts: dict[str, int] = {}
        for rec in self._store.iter_all():
            counts[rec.source] = counts.get(rec.source, 0) + 1
        return counts

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._store.close()
        except Exception:
            pass


__all__ = ["LongTermMemory", "LongTermConfig", "DEFAULT_MIN_CONTENT_CHARS"]
