"""VectorStore factory — selects and builds the vector store backend.

Two backends:

- ``sqlite`` (default): :class:`~markbot.memory.vectorstore.SQLiteVectorStore`,
  zero extra dependencies (standard library only).
- ``chroma``: :class:`~markbot.memory.vectorstores.ChromaVectorStore`,
  requires ``pip install markbot[chroma]``. Wraps ChromaDB but injects
  our :class:`~markbot.memory.embedder.Embedder` so embeddings are
  consistent and switchable (rather than Chroma's bundled default
  model).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from .embedder import Embedder
from .vectorstore import InMemoryVectorStore, SQLiteVectorStore, VectorStore


def build_vectorstore(
    config: dict[str, Any],
    embedder: Embedder,
    working_dir: str | Path,
) -> VectorStore:
    """Build the configured vector store.

    Args:
        config: Config dict. Recognized keys:
            - ``vector_backend``: ``"sqlite"`` (default) or ``"chroma"``.
            - ``vector_max_records``: LRU cap (sqlite only).
            - ``provider_config``: backend-specific options (chroma:
              ``host``, ``port``, ``persist_dir``, ``collection``,
              ``mode``).
        embedder: The embedder whose vectors will be stored. Chroma is
            configured to use this for embedding so backends stay
            consistent.
        working_dir: Workspace root, used for the default SQLite path
            (``<working_dir>/memory/.vectors.db``).

    Returns:
        A ready-to-use :class:`VectorStore`.

    Falls back to :class:`SQLiteVectorStore` if the requested backend
    is unavailable (e.g. chromadb not installed).
    """
    backend = str(config.get("vector_backend", "sqlite")).lower().strip()
    working_dir = Path(working_dir)
    max_records = int(config.get("vector_max_records", 50_000))
    max_scan_records = int(config.get("vector_max_scan_records", 20_000))

    if backend == "chroma":
        store = _try_chroma(config, embedder, working_dir)
        if store is not None:
            return store
        logger.warning(
            "vector_backend='chroma' requested but unavailable; "
            "falling back to sqlite. Install with: pip install 'markbot[chroma]'"
        )

    if backend == "memory":
        # Undocumented in config but handy for tests.
        return InMemoryVectorStore()

    # Default: SQLite.
    db_path = working_dir / "memory" / ".vectors.db"
    return SQLiteVectorStore(db_path, max_records=max_records, max_scan_records=max_scan_records)


def _try_chroma(
    config: dict[str, Any],
    embedder: Embedder,
    working_dir: Path,
) -> VectorStore | None:
    """Attempt to build the Chroma backend; return None if unavailable."""
    # Implementation lives in markbot.memory.vectorstores (package __init__),
    # not a missing vectorstores.chroma submodule.
    try:
        from .vectorstores import ChromaVectorStore
    except ImportError:
        logger.debug("chromadb not installed; ChromaVectorStore unavailable")
        return None
    try:
        provider_cfg = config.get("provider_config") or {}
        store = ChromaVectorStore(
            embedder=embedder,
            working_dir=working_dir,
            persist_dir=provider_cfg.get("persist_dir", ""),
            host=provider_cfg.get("host", ""),
            port=provider_cfg.get("port", 0),
            mode=provider_cfg.get("mode", "local"),
            collection=provider_cfg.get("collection", "markbot_memory"),
        )
        logger.info(
            "VectorStore: ChromaDB backend (collection='{}', mode={})",
            provider_cfg.get("collection", "markbot_memory"),
            provider_cfg.get("mode", "local"),
        )
        return store
    except Exception as exc:
        logger.warning("ChromaVectorStore construction failed: {}", exc)
        return None


__all__ = ["build_vectorstore"]
