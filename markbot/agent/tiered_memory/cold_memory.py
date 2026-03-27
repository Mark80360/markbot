"""L4 Cold Memory - Long-term semantic storage using QMD (Queryable Memory Database)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    # Try to import sentence-transformers for embeddings
    from sentence_transformers import SentenceTransformer
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False

try:
    # Try to import chromadb for vector storage
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False

from .base import BaseMemoryLayer, MemoryTier


class ColdMemory(BaseMemoryLayer):
    """
    L4 Cold Memory: Long-term semantic storage using QMD.
    
    - Stores archived memories from Hot + Warm compression
    - Uses vector embeddings for semantic search
    - Persistent across sessions
    - Collections for organizing different types of memory
    
    Implementation:
    - Primary: ChromaDB with sentence-transformers embeddings
    - Fallback: JSONL file with basic keyword search
    """
    
    def __init__(self, workspace_path: str, embedding_model: str = "all-MiniLM-L6-v2"):
        super().__init__(MemoryTier.COLD, workspace_path)
        self.workspace_path = workspace_path
        self.db_path = Path(workspace_path) / "memory" / "cold"
        self.db_path.mkdir(parents=True, exist_ok=True)
        
        self.embedding_model_name = embedding_model
        self._embedding_model = None
        self._chroma_client = None
        self._collection = None
        
        # Fallback storage
        self._fallback_file = self.db_path / "memories.jsonl"
        
        # Initialize if dependencies available
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize vector database."""
        if not CHROMADB_AVAILABLE or not EMBEDDINGS_AVAILABLE:
            print(f"[ColdMemory] Vector DB not available, using fallback storage")
            return
        
        try:
            # Initialize ChromaDB
            self._chroma_client = chromadb.Client(Settings(
                persist_directory=str(self.db_path),
                anonymized_telemetry=False
            ))
            
            # Get or create collection
            self._collection = self._chroma_client.get_or_create_collection(
                name="cold_memory",
                metadata={"hnsw:space": "cosine"}
            )
            
            # Initialize embedding model
            self._embedding_model = SentenceTransformer(self.embedding_model_name)
            
            print(f"[ColdMemory] Initialized with {self.embedding_model_name}")
            
        except Exception as e:
            print(f"[ColdMemory] Failed to initialize vector DB: {e}")
            self._chroma_client = None
            self._collection = None
    
    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Get embedding for text."""
        if self._embedding_model is None:
            return None
        try:
            return self._embedding_model.encode(text).tolist()
        except Exception as e:
            print(f"[ColdMemory] Embedding error: {e}")
            return None
    
    def add_document(self, content: str, collection: str = "core_memory", 
                     metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Add a document to cold memory.
        
        Args:
            content: Document content
            collection: Collection name for organization
            metadata: Additional metadata
        """
        doc_id = f"{collection}_{int(time.time() * 1000)}"
        meta = metadata or {}
        meta.update({
            "collection": collection,
            "timestamp": time.time(),
            "content_preview": content[:200]
        })
        
        # Try vector DB first
        if self._collection is not None:
            try:
                embedding = self._get_embedding(content)
                if embedding:
                    self._collection.add(
                        ids=[doc_id],
                        documents=[content],
                        embeddings=[embedding],
                        metadatas=[meta]
                    )
                    return True
            except Exception as e:
                print(f"[ColdMemory] Vector add error: {e}")
        
        # Fallback to JSONL
        try:
            entry = {
                "id": doc_id,
                "content": content,
                "metadata": meta
            }
            with open(self._fallback_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            print(f"[ColdMemory] Fallback add error: {e}")
            return False
    
    def search(self, query: str, limit: int = 5, 
               collection: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Semantic search in cold memory.
        
        Args:
            query: Search query
            limit: Max results
            collection: Filter by collection (optional)
            
        Returns:
            List of matching documents with scores
        """
        results = []
        
        # Try vector search first
        if self._collection is not None:
            try:
                query_embedding = self._get_embedding(query)
                if query_embedding:
                    where_filter = {"collection": collection} if collection else None
                    
                    search_results = self._collection.query(
                        query_embeddings=[query_embedding],
                        n_results=limit,
                        where=where_filter
                    )
                    
                    if search_results["ids"]:
                        for i, doc_id in enumerate(search_results["ids"][0]):
                            result = {
                                "id": doc_id,
                                "content": search_results["documents"][0][i],
                                "distance": search_results["distances"][0][i],
                                "metadata": search_results["metadatas"][0][i]
                            }
                            results.append(result)
                        
                        return results
                        
            except Exception as e:
                print(f"[ColdMemory] Vector search error: {e}")
        
        # Fallback: keyword search in JSONL
        if self._fallback_file.exists():
            try:
                query_lower = query.lower()
                with open(self._fallback_file, "r", encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line.strip())
                        content = entry.get("content", "").lower()
                        
                        # Check collection filter
                        if collection:
                            entry_collection = entry.get("metadata", {}).get("collection")
                            if entry_collection != collection:
                                continue
                        
                        # Simple keyword matching
                        if any(term in content for term in query_lower.split()):
                            results.append({
                                "id": entry["id"],
                                "content": entry["content"],
                                "distance": 0.5,  # Placeholder score
                                "metadata": entry.get("metadata", {})
                            })
                            
                            if len(results) >= limit:
                                break
                                
            except Exception as e:
                print(f"[ColdMemory] Fallback search error: {e}")
        
        return results
    
    def get_collection_stats(self, collection: str = "core_memory") -> Dict[str, Any]:
        """Get statistics for a collection."""
        stats = {
            "collection": collection,
            "total_documents": 0,
            "storage_type": "unknown"
        }
        
        if self._collection is not None:
            try:
                # This is a simplified count, actual count may vary
                stats["storage_type"] = "chromadb"
                stats["total_documents"] = self._collection.count()
            except Exception:
                pass
        
        if self._fallback_file.exists():
            try:
                count = 0
                with open(self._fallback_file, "r", encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line.strip())
                        if entry.get("metadata", {}).get("collection") == collection:
                            count += 1
                stats["fallback_count"] = count
            except Exception:
                pass
        
        return stats
    
    def compact_from_hot_and_warm(self, hot_entries: List[str], 
                                   warm_entries: List[Dict]) -> int:
        """
        Compress Hot and Warm entries into Cold memory.
        
        Args:
            hot_entries: List of hot memory entries (strings)
            warm_entries: List of warm memory entries (dicts with content)
            
        Returns:
            Number of documents added
        """
        added = 0
        
        # Process hot entries as facts
        for entry in hot_entries[-10:]:  # Last 10 hottest
            if self.add_document(entry, collection="hot_facts"):
                added += 1
        
        # Process warm entries as conversations
        for entry in warm_entries[-5:]:  # Last 5 conversations
            content = entry.get("content", "")
            if content and self.add_document(content, collection="conversations"):
                added += 1
        
        return added
    
    # BaseMemoryLayer interface
    
    def add(self, content: str, **metadata) -> None:
        """Add content to cold memory."""
        collection = metadata.get("collection", "core_memory")
        self.add_document(content, collection=collection, metadata=metadata)
    
    def get_context(self, query: Optional[str] = None, limit: int = 10) -> str:
        """Get relevant context from cold memory."""
        if not query:
            return ""
        
        results = self.search(query, limit=limit)
        if not results:
            return ""
        
        lines = ["## Long-term Memory (Semantic Search)"]
        for result in results:
            content = result.get("content", "")[:500]
            score = result.get("distance", 0)
            lines.append(f"\n[Relevance: {1-score:.2f}] {content}")
        
        return "\n".join(lines)
    
    def clear(self) -> None:
        """Clear all cold memory (use with caution!)."""
        # Clear ChromaDB
        if self._collection is not None:
            try:
                self._collection.delete(where={})
            except Exception as e:
                print(f"[ColdMemory] Clear error: {e}")
        
        # Clear fallback file
        if self._fallback_file.exists():
            os.remove(self._fallback_file)
    
    @property
    def is_persistent(self) -> bool:
        return True
    
    def is_available(self) -> bool:
        """Check if vector DB is available."""
        return self._collection is not None
