"""Optional ChromaDB vector store adapter.

Wraps ChromaDB but injects our :class:`~markbot.memory.embedder.Embedder`
for embedding, so:

- embeddings are consistent with the rest of the memory stack (the
  SQLite backend uses the same embedder),
- switching the embedder (e.g. OpenAI → local model) doesn't silently
  change what Chroma stores,
- we avoid Chroma's bundled ONNX model being downloaded behind the
  user's back.

Requires ``pip install markbot[chroma]`` (chromadb).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from loguru import logger

from ..embedder import Embedder
from ..vectorstore import VectorRecord, VectorStore, _normalize

try:
    import chromadb  # type: ignore
    from chromadb.api.types import EmbeddingFunction, Documents, Embeddings  # type: ignore
    _HAS_CHROMA = True
except ImportError:
    chromadb = None  # type: ignore[assignment]
    _HAS_CHROMA = False


class _EmbedderFunction(EmbeddingFunction):  # type: ignore[misc]
    """Adapter so Chroma calls our :class:`Embedder` for embeddings.

    Chroma wants an object with a ``__call__(input: Documents) ->
    Embeddings`` method. We wrap our embedder so Chroma stores and
    queries with the same vectors the rest of the stack uses.
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - Chroma's API
        vecs = self._embedder.embed(list(input))
        # Chroma accepts plain lists of floats.
        return vecs

    def name(self) -> str:
        return "markbot-embedder"


class ChromaVectorStore(VectorStore):
    """ChromaDB-backed vector store.

    All records are stored in a single collection. Cosine similarity is
    configured via ``metadata={"hnsw:space": "cosine"}`` so Chroma
    normalizes for us. The ``metadata`` dict on each record is stored in
    Chroma's per-document metadata (JSON-encoded values where needed,
    since Chroma metadata values must be primitives).
    """

    def __init__(
        self,
        *,
        embedder: Embedder,
        working_dir: Path,
        persist_dir: str = "",
        host: str = "",
        port: int = 0,
        mode: str = "local",
        collection: str = "markbot_memory",
    ) -> None:
        if not _HAS_CHROMA:
            raise ImportError(
                "chromadb is not installed; install with: pip install 'markbot[chroma]'"
            )
        self._embedder = embedder
        self._working_dir = working_dir
        self._collection_name = collection
        self._lock = threading.Lock()

        emb_fn = _EmbedderFunction(embedder)

        if mode == "remote" and host:
            self._client = chromadb.HttpClient(host=host, port=port or 8000)
            logger.info("ChromaVectorStore: remote {}:{}", host, port or 8000)
        elif persist_dir:
            self._client = chromadb.PersistentClient(path=persist_dir)
            logger.info("ChromaVectorStore: local persistent at {}", persist_dir)
        else:
            # Default to a workspace-local persistent path so data survives.
            default_path = str(working_dir / "memory" / ".chroma")
            Path(default_path).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=default_path)
            logger.info("ChromaVectorStore: local persistent at {}", default_path)

        self._collection_obj = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
            embedding_function=emb_fn,
        )

        # Track embedding sig in Chroma collection metadata.
        try:
            existing_meta = self._collection_obj.metadata or {}
        except Exception:
            existing_meta = {}
        self._sig = existing_meta.get("embedding_sig")

    # -- meta helpers --------------------------------------------------------

    @property
    def embedding_sig(self) -> str | None:
        return self._sig

    @embedding_sig.setter
    def embedding_sig(self, value: str | None) -> None:
        self._sig = value
        try:
            # Merge into existing collection metadata.
            meta = dict(self._collection_obj.metadata or {})
            if value is None:
                meta.pop("embedding_sig", None)
            else:
                meta["embedding_sig"] = value
            self._collection_obj.modify(metadata=meta)
        except Exception as exc:
            logger.debug("Chroma embedding_sig set failed: {}", exc)

    # -- core ops ------------------------------------------------------------

    def upsert(self, record: VectorRecord) -> None:
        rid = record.id
        meta = self._encode_meta(record.metadata)
        created = record.created_at or time.time()
        meta["created_at"] = created
        meta["access_count"] = int(record.access_count)
        with self._lock:
            # We pass the embedding explicitly so Chroma uses OUR vector,
            # not a re-embedding of the content (which would double the
            # work and could diverge for backends with nondeterministic
            # batching).
            self._collection_obj.upsert(
                ids=[rid],
                documents=[record.content],
                embeddings=[_normalize(record.vector)],
                metadatas=[meta],
            )

    def query(
        self,
        query_vec: list[float],
        top_k: int = 10,
        *,
        source: str | None = None,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[VectorRecord]:
        where: dict[str, Any] = {}
        if source is not None:
            where["source"] = source
        if filter_metadata:
            for k, v in filter_metadata.items():
                # Values must be JSON-encoded if non-primitive.
                where[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)

        try:
            res = self._collection_obj.query(
                query_embeddings=[_normalize(query_vec)],
                n_results=top_k,
                where=where or None,
            )
        except Exception as exc:
            logger.debug("Chroma query failed: {}", exc)
            return []

        out: list[VectorRecord] = []
        if not res or not res.get("ids") or not res["ids"][0]:
            return out
        ids = res["ids"][0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for rid, doc, meta, dist in zip(ids, docs, metas, dists):
            decoded = self._decode_meta(meta)
            # Chroma cosine distance = 1 - similarity.
            score = max(0.0, 1.0 - float(dist))
            out.append(VectorRecord(
                id=rid,
                content=doc or "",
                vector=[],  # not returned by Chroma; not needed for display
                source=decoded.get("source", ""),
                metadata=decoded,
                score=score,
                created_at=float(decoded.get("created_at", 0.0)),
                access_count=int(decoded.get("access_count", 0)),
            ))
        return out

    def get(self, record_id: str) -> VectorRecord | None:
        try:
            res = self._collection_obj.get(ids=[record_id])
        except Exception:
            return None
        if not res or not res.get("ids"):
            return None
        rid = res["ids"][0]
        doc = (res.get("documents") or [""])[0]
        meta = self._decode_meta((res.get("metadatas") or [{}])[0])
        return VectorRecord(
            id=rid, content=doc or "", vector=[], source=meta.get("source", ""),
            metadata=meta, created_at=float(meta.get("created_at", 0.0)),
            access_count=int(meta.get("access_count", 0)),
        )

    def delete(self, record_id: str) -> bool:
        existing = self.get(record_id)
        if existing is None:
            return False
        try:
            self._collection_obj.delete(ids=[record_id])
            return True
        except Exception:
            return False

    def delete_by_source(self, source: str) -> int:
        try:
            res = self._collection_obj.get(where={"source": source})
        except Exception:
            return 0
        ids = (res or {}).get("ids") or []
        if not ids:
            return 0
        try:
            self._collection_obj.delete(ids=ids)
            return len(ids)
        except Exception:
            return 0

    def count(self) -> int:
        try:
            return int(self._collection_obj.count())
        except Exception:
            return 0

    def iter_all(self) -> list[VectorRecord]:
        try:
            res = self._collection_obj.get()
        except Exception:
            return []
        ids = (res or {}).get("ids") or []
        docs = (res or {}).get("documents") or []
        metas = (res or {}).get("metadatas") or []
        out: list[VectorRecord] = []
        for rid, doc, meta in zip(ids, docs, metas):
            decoded = self._decode_meta(meta)
            out.append(VectorRecord(
                id=rid, content=doc or "", vector=[], source=decoded.get("source", ""),
                metadata=decoded, created_at=float(decoded.get("created_at", 0.0)),
                access_count=int(decoded.get("access_count", 0)),
            ))
        return out

    def increment_access(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        with self._lock:
            for rid in record_ids:
                try:
                    res = self._collection_obj.get(ids=[rid])
                    if not res or not res.get("ids"):
                        continue
                    meta = self._decode_meta((res.get("metadatas") or [{}])[0])
                    doc = (res.get("documents") or [""])[0]
                    new_count = int(meta.get("access_count", 0)) + 1
                    meta["access_count"] = new_count
                    self._collection_obj.update(
                        ids=[rid],
                        documents=[doc],
                        metadatas=[self._encode_meta(meta)],
                    )
                except Exception as exc:
                    logger.debug("Chroma increment_access failed for {}: {}", rid, exc)

    def clear(self) -> None:
        with self._lock:
            try:
                self._client.delete_collection(self._collection_name)
            except Exception:
                pass
            emb_fn = _EmbedderFunction(self._embedder)
            self._collection_obj = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=emb_fn,
            )
        self._sig = None

    def close(self) -> None:
        # Chroma's PersistentClient flushes on every op; nothing to do.
        pass

    # -- metadata (de)serialization -----------------------------------------

    @staticmethod
    def _encode_meta(meta: dict[str, Any]) -> dict[str, Any]:
        """Flatten metadata to Chroma-compatible primitives."""
        out: dict[str, Any] = {}
        for k, v in meta.items():
            if isinstance(v, (str, int, float, bool)):
                out[k] = v
            elif v is None:
                continue
            else:
                out[k] = json.dumps(v, ensure_ascii=False, default=str)
        return out

    @staticmethod
    def _decode_meta(meta: dict[str, Any]) -> dict[str, Any]:
        """Best-effort decode of JSON-encoded metadata values."""
        if not meta:
            return {}
        out: dict[str, Any] = {}
        for k, v in meta.items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                try:
                    out[k] = json.loads(v)
                    continue
                except Exception:
                    pass
            out[k] = v
        return out


__all__ = ["ChromaVectorStore"]
