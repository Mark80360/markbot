"""ChromaDB memory provider — reference implementation of MemoryProvider.

Uses chromadb for vector-based semantic memory storage and retrieval.
Supports both local persistent and remote HTTP modes.

Requires: pip install markbot[chroma]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from markbot.memory.provider import MemoryProvider


class ChromaMemoryProvider(MemoryProvider):
    """ChromaDB-backed memory provider.

    Configuration (passed via plugin_config):
      - host: ChromaDB server host (default: localhost)
      - port: ChromaDB server port (default: 8000)
      - collection: Collection name (default: "markbot_memory")
      - persist_dir: Local persistent directory (for local mode)
      - mode: "local" or "remote" (default: "local")
    """

    def __init__(self):
        self._client = None
        self._collection = None
        self._config: Dict[str, Any] = {}
        self._initialized = False

    @property
    def name(self) -> str:
        return "chroma"

    def is_available(self) -> bool:
        """Check if chromadb is installed."""
        try:
            import chromadb  # noqa: F401
            return True
        except ImportError:
            logger.debug("chromadb not installed, ChromaMemoryProvider unavailable")
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize ChromaDB client and collection.

        Config is read from kwargs (preferred) or _plugin_config (set by discovery).
        """
        if self._initialized:
            return

        self._config = dict(getattr(self, '_plugin_config', {}))
        self._config.update(kwargs)

        try:
            import chromadb

            mode = self._config.get("mode", "local")

            if mode == "remote":
                host = self._config.get("host", "localhost")
                port = self._config.get("port", 8000)
                self._client = chromadb.HttpClient(host=host, port=port)
                logger.info("Connected to ChromaDB at {}:{}", host, port)
            else:
                persist_dir = self._config.get("persist_dir", "")
                if persist_dir:
                    self._client = chromadb.PersistentClient(path=persist_dir)
                else:
                    self._client = chromadb.Client()
                logger.info("Initialized local ChromaDB")

            collection_name = self._config.get("collection", "markbot_memory")
            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            self._initialized = True
            logger.info("ChromaDB collection '{}' ready", collection_name)

        except ImportError:
            logger.error("chromadb is not installed")
        except Exception as e:
            logger.error("Failed to initialize ChromaDB: {}", e)

    def store(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Store a memory entry in ChromaDB.

        Returns:
            The ID of the stored document.
        """
        if not self._collection:
            return ""

        import hashlib
        doc_id = hashlib.sha256(content.encode()).hexdigest()[:16]

        meta = metadata or {}
        meta.setdefault("type", "memory")
        meta.setdefault("timestamp", "")

        try:
            self._collection.upsert(
                ids=[doc_id],
                documents=[content],
                metadatas=[meta],
            )
            return doc_id
        except Exception as e:
            logger.error("ChromaDB store failed: {}", e)
            return ""

    def query(self, query_text: str, n_results: int = 5) -> List[Dict[str, Any]]:
        """Query ChromaDB for relevant memories.

        Returns:
            List of dicts with 'id', 'content', 'metadata', 'distance' keys.
        """
        if not self._collection:
            return []

        try:
            results = self._collection.query(
                query_texts=[query_text],
                n_results=n_results,
            )

            memories = []
            if results and results["documents"]:
                for i, doc in enumerate(results["documents"][0]):
                    doc_id = results["ids"][0][i] if results["ids"] else ""
                    meta = results["metadatas"][0][i] if results["metadatas"] else {}
                    dist = results["distances"][0][i] if results["distances"] else 0.0
                    memories.append({
                        "id": doc_id,
                        "content": doc,
                        "metadata": meta,
                        "distance": dist,
                    })
            return memories
        except Exception as e:
            logger.error("ChromaDB query failed: {}", e)
            return []

    def update(self, doc_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Update an existing memory entry."""
        if not self._collection:
            return False

        try:
            meta = metadata or {}
            self._collection.update(
                ids=[doc_id],
                documents=[content],
                metadatas=[meta],
            )
            return True
        except Exception as e:
            logger.error("ChromaDB update failed: {}", e)
            return False

    def delete(self, doc_id: str) -> bool:
        """Delete a memory entry."""
        if not self._collection:
            return False

        try:
            self._collection.delete(ids=[doc_id])
            return True
        except Exception as e:
            logger.error("ChromaDB delete failed: {}", e)
            return False

    def count(self) -> int:
        """Return the number of stored memories."""
        if not self._collection:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def system_prompt_block(self) -> str:
        """Return a brief status line for the system prompt."""
        if not self._initialized:
            return ""
        count = self.count()
        return f"[Memory: ChromaDB active, {count} entries stored]"

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn."""
        if not self._initialized or not query:
            return ""

        results = self.query(query, n_results=3)
        if not results:
            return ""

        parts = []
        for r in results:
            parts.append(f"- {r['content']}")
        return "Relevant memories:\n" + "\n".join(parts)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist the turn to ChromaDB."""
        if not self._initialized:
            return

        # Store user message
        self.store(
            user_content,
            metadata={"type": "user_message", "session_id": session_id},
        )
        # Store assistant response
        self.store(
            assistant_content,
            metadata={"type": "assistant_response", "session_id": session_id},
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to ChromaDB."""
        if not self._initialized:
            return

        if action in ("add", "replace"):
            merged = dict(metadata) if metadata else {}
            merged["type"] = f"memory_{target}"
            if action == "replace":
                # Remove old entry first
                old_results = self.query(content, n_results=1)
                if old_results and old_results[0].get("distance", 1.0) < 0.1:
                    old_id = old_results[0].get("id", "")
                    if old_id:
                        self.delete(old_id)
            self.store(content, metadata=merged)
        elif action == "remove":
            # Find and delete matching content
            results = self.query(content, n_results=1)
            if results and results[0].get("distance", 1.0) < 0.1:
                doc_id = results[0].get("id", "")
                if doc_id:
                    self.delete(doc_id)

    def shutdown(self) -> None:
        """Clean up resources."""
        self._initialized = False
        self._collection = None
        self._client = None


__all__ = ["ChromaMemoryProvider"]
