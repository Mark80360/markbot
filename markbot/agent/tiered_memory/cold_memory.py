"""L4 Cold Memory - Long-term semantic storage with vector database.

Stores knowledge as structured entries with semantic search capability.
Uses ChromaDB for vector similarity search with sentence-transformers embeddings.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class KnowledgeType(Enum):
    """Types of knowledge stored in Cold Memory."""
    
    FACT = "FACT"                         # Factual information (tech facts, concepts)
    EXPERIENCE = "EXPERIENCE"             # Learned patterns from past work
    PREFERENCE = "PREFERENCE"             # User preferences and habits
    DECISION = "DECISION"                 # Important decisions made
    ERROR_PATTERN = "ERROR_PATTERN"       # Error solutions and workarounds
    CODE_PATTERN = "CODE_PATTERN"         # Code patterns and best practices
    PROJECT_CONTEXT = "PROJECT_CONTEXT"   # Project-specific context


@dataclass
class ColdMemoryEntry:
    """Structured entry for Cold Memory (L4) with full metadata."""
    
    entry_id: str                          # Unique identifier
    content: str                           # Main text content
    knowledge_type: KnowledgeType          # Type classification
    
    source_session: str = ""               # Origin session ID
    source_date: str = ""                  # Date when created (YYYY-MM-DD)
    
    tags: List[str] = field(default_factory=list)     # Searchable keywords
    confidence: float = 0.8                # Extraction confidence (0-1)
    
    access_count: int = 0                  # Access frequency
    last_accessed: Optional[datetime] = None          # Last retrieval time
    
    related_entries: List[str] = field(default_factory=list)  # IDs of related entries
    metadata: Dict[str, Any] = field(default_factory=dict)   # Additional metadata
    
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            **asdict(self),
            'knowledge_type': self.knowledge_type.value,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_accessed': self.last_accessed.isoformat() if self.last_accessed else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ColdMemoryEntry':
        """Create instance from dictionary."""
        if isinstance(data.get('knowledge_type'), str):
            data['knowledge_type'] = KnowledgeType(data['knowledge_type'])
        
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if isinstance(data.get('updated_at'), str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        if isinstance(data.get('last_accessed'), str):
            data['last_accessed'] = datetime.fromisoformat(data['last_accessed'])
        
        return cls(**data)
    
    def generate_embedding_text(self) -> str:
        """
        Generate text for embedding generation.
        
        Combines content, tags, and type for better semantic representation.
        """
        parts = [self.content]
        
        if self.tags:
            parts.append(" ".join(self.tags))
        
        parts.append(f"[{self.knowledge_type.value}]")
        
        return " ".join(parts)


class ColdMemory:
    """
    L4 Cold Memory: Long-term semantic storage.
    
    Purpose:
    - Store structured knowledge that persists indefinitely
    - Enable semantic search across all historical data
    - Support knowledge reuse across projects/sessions
    
    Storage Architecture:
    - Primary: ChromaDB vector database (workspace/memory/cold_db/)
    - Backup: JSON files (workspace/memory/cold_backup/)
    - Index: Metadata index file (workspace/memory/COLD_INDEX.json)
    
    Key Features:
    - Semantic similarity search using sentence-transformers
    - Structured knowledge types (7 categories)
    - Automatic deduplication via content hashing
    - Access tracking for popularity ranking
    """
    
    DEFAULT_COLLECTION_NAME = "markbot_knowledge"
    EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # Good balance of speed/quality
    
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path)
        self.db_path = self.workspace_path / "memory" / "cold_db"
        self.backup_path = self.workspace_path / "memory" / "cold_backup"
        self.index_file = self.workspace_path / "memory" / "COLD_INDEX.json"
        
        # Ensure directories exist
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.backup_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize components
        self._collection = None
        self._embedding_model = None
        self._index: Dict[str, Dict[str, Any]] = {}
        
        # Statistics
        self._total_stored = 0
        self._total_searches = 0
        
        # Lazy initialization flag
        self._initialized = False
        
        logger.info(f"[ColdMemory] Initialized at {self.db_path}")
    
    def _ensure_initialized(self) -> None:
        """Lazy initialization of database connection."""
        if self._initialized:
            return
        
        try:
            import chromadb
            
            client = chromadb.PersistentClient(path=str(self.db_path))
            
            self._collection = client.get_or_create_collection(
                name=self.DEFAULT_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"}
            )
            
            logger.info(
                f"[ColdMemory] Connected to collection: "
                f"{self._collection.count()} existing entries"
            )
            
            # Load index
            self._load_index()
            
            self._initialized = True
            
        except ImportError:
            logger.warning(
                "[ColdMemory] ChromaDB not installed. "
                "Using fallback mode (JSON only)."
            )
            self._initialized = True
        except Exception as e:
            logger.error(f"[ColdMemory] Initialization failed: {e}")
            raise
    
    def _get_embedding_model(self):
        """Get or initialize the sentence-transformers model."""
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._embedding_model = SentenceTransformer(self.EMBEDDING_MODEL)
                logger.info(f"[ColdMemory] Loaded model: {self.EMBEDDING_MODEL}")
            except ImportError:
                logger.warning(
                    "[ColdMemory] sentence-transformers not installed. "
                    "Using simple text hashing fallback."
                )
                return None
        
        return self._embedding_model
    
    def _generate_embeddings(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Generate embeddings for a list of texts."""
        model = self._get_embedding_model()
        
        if model is None:
            return None
        
        try:
            embeddings = model.encode(texts, show_progress_bar=False)
            return embeddings.tolist()
        except Exception as e:
            logger.error(f"[ColdMemory] Embedding generation failed: {e}")
            return None
    
    def _content_hash(self, content: str) -> str:
        """Generate deterministic hash for content deduplication."""
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def add_entry(
        self,
        content: str,
        knowledge_type: Union[str, KnowledgeType] = KnowledgeType.FACT,
        tags: Optional[List[str]] = None,
        source_session: str = "",
        confidence: float = 0.8,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add a new knowledge entry to Cold Memory.
        
        Args:
            content: The knowledge content (structured text)
            knowledge_type: One of the 7 knowledge types
            tags: Searchable keywords for this entry
            source_session: Origin session ID
            confidence: Extraction confidence (0-1)
            metadata: Additional metadata
            
        Returns:
            True if successfully added
            False if duplicate or error
        """
        self._ensure_initialized()
        
        # Normalize inputs
        content = content.strip()
        if len(content) < 20:
            logger.debug(f"[ColdMemory] Content too short ({len(content)} chars)")
            return False
        
        if isinstance(knowledge_type, str):
            try:
                knowledge_type = KnowledgeType(knowledge_type.upper())
            except ValueError:
                logger.warning(f"[ColdMemory] Unknown type '{knowledge_type}', defaulting to FACT")
                knowledge_type = KnowledgeType.FACT
        
        # Check for duplicates
        content_hash = self._content_hash(content)
        if content_hash in self._index:
            logger.debug(f"[ColdMemory] Duplicate detected: {content_hash[:8]}...")
            return False
        
        # Create entry object
        entry_id = str(uuid.uuid4())[:12]
        entry = ColdMemoryEntry(
            entry_id=entry_id,
            content=content[:2000],  # Limit content length
            knowledge_type=knowledge_type,
            source_session=source_session,
            source_date=time.strftime("%Y-%m-%d"),
            tags=tags or [],
            confidence=min(max(confidence, 0.0), 1.0),
            metadata=metadata or {},
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        
        # Generate embedding
        embedding_text = entry.generate_embedding_text()
        embeddings = self._generate_embeddings([embedding_text])
        
        # Try vector DB first
        success = False
        
        if self._collection is not None and embeddings:
            try:
                self._collection.add(
                    ids=[entry_id],
                    documents=[content],
                    embeddings=[embeddings[0]],
                    metadatas=[entry.to_dict()]
                )
                success = True
                
            except Exception as e:
                logger.warning(f"[ColdMemory] Vector DB write failed: {e}, falling back to JSON")
        
        # Fallback to JSON backup
        if not success:
            self._save_entry_to_json(entry)
            success = True
        
        # Update index
        self._index[content_hash] = {
            "entry_id": entry_id,
            "type": knowledge_type.value,
            "date": entry.source_date,
            "tags": tags or [],
            "content_preview": content[:100],
        }
        
        self._save_index()
        self._total_stored += 1
        
        logger.info(
            f"[ColdMemory] ✓ Added [{knowledge_type.value}] "
            f"(#{self._total_stored}): {entry.entry_id} - "
            f"{content[:60]}..."
        )
        
        return True
    
    def _save_entry_to_json(self, entry: ColdMemoryEntry) -> None:
        """Save entry to JSON backup file."""
        filename = f"{entry.entry_id}.json"
        filepath = self.backup_path / filename
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(entry.to_dict(), f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f"[ColdMemory] Failed to save JSON backup: {e}")
    
    def _load_index(self) -> None:
        """Load metadata index from disk."""
        if self.index_file.exists():
            try:
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    self._index = json.load(f)
                    
                logger.debug(f"[ColdMemory] Loaded index with {len(self._index)} entries")
                
            except Exception as e:
                logger.warning(f"[ColdMemory] Failed to load index: {e}")
                self._index = {}
    
    def _save_index(self) -> None:
        """Save metadata index to disk."""
        try:
            with open(self.index_file, 'w', encoding='utf-8') as f:
                json.dump(self._index, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f"[ColdMemory] Failed to save index: {e}")
    
    def semantic_search(
        self,
        query: str,
        limit: int = 5,
        min_similarity: float = 0.3,
        knowledge_types: Optional[List[KnowledgeType]] = None
    ) -> List[Dict[str, Any]]:
        """
        Perform semantic similarity search.
        
        Uses vector embeddings to find semantically similar entries.
        Falls back to keyword matching if embeddings unavailable.
        
        Args:
            query: Natural language search query
            limit: Maximum results to return
            min_similarity: Minimum cosine similarity threshold
            knowledge_types: Filter by specific knowledge types
            
        Returns:
            List of result dicts with entry data and similarity scores
        """
        self._ensure_initialized()
        self._total_searches += 1
        
        # Vector-based search (primary method)
        if self._collection is not None:
            results = self._vector_search(query, limit * 2, min_similarity)
            
            if knowledge_types:
                results = [
                    r for r in results 
                    if r.get('metadata', {}).get('knowledge_type') in 
                    [kt.value for kt in knowledge_types]
                ]
            
            return results[:limit]
        
        # Fallback: keyword-based search
        return self._keyword_search(query, limit, knowledge_types)
    
    def _vector_search(
        self,
        query: str,
        limit: int,
        min_similarity: float
    ) -> List[Dict[str, Any]]:
        """Perform vector similarity search."""
        try:
            # Generate query embedding
            embeddings = self._generate_embeddings([query])
            if not embeddings:
                return []
            
            # Query collection
            results = self._collection.query(
                query_embeddings=[embeddings[0]],
                n_results=min(limit, self._collection.count()),
                include=["documents", "metadatas", "distances"]
            )
            
            # Format results
            formatted = []
            for i, doc in enumerate(results["documents"][0]):
                distance = results["distances"][0][i]
                similarity = 1 - distance  # Convert distance to similarity
                
                if similarity >= min_similarity:
                    formatted.append({
                        "id": results["ids"][0][i],
                        "content": doc,
                        "similarity": round(similarity, 3),
                        "metadata": results["metadatas"][0][i],
                    })
                    
                    # Update access tracking
                    entry_id = results["ids"][0][i]
                    if entry_id in self._index:
                        self._index[entry_id]["access_count"] = \
                            self._index[entry_id].get("access_count", 0) + 1
            
            # Sort by similarity descending
            formatted.sort(key=lambda x: x["similarity"], reverse=True)
            
            logger.debug(
                f"[ColdMemory] Vector search returned {len(formatted)} results"
            )
            
            return formatted
            
        except Exception as e:
            logger.error(f"[ColdMemory] Vector search failed: {e}")
            return []
    
    def _keyword_search(
        self,
        query: str,
        limit: int,
        knowledge_types: Optional[List[KnowledgeType]]
    ) -> List[Dict[str, Any]]:
        """Fallback keyword-based search."""
        query_lower = query.lower()
        query_words = set(query_lower.split())
        
        results = []
        
        for content_hash, meta in self._index.items():
            # Type filter
            if knowledge_types:
                if meta.get("type") not in [kt.value for kt in knowledge_types]:
                    continue
            
            # Keyword matching
            searchable = (
                f"{meta.get('content_preview', '')} "
                f"{' '.join(meta.get('tags', []))}"
            ).lower()
            
            matches = sum(1 for word in query_words if word in searchable)
            
            if matches > 0:
                results.append({
                    "id": meta.get("entry_id", ""),
                    "content": meta.get("content_preview", ""),
                    "similarity": matches / len(query_words),
                    "metadata": meta,
                })
        
        # Sort by match count
        results.sort(key=lambda x: x["similarity"], reverse=True)
        
        return results[:limit]
    
    def get_by_id(self, entry_id: str) -> Optional[ColdMemoryEntry]:
        """Retrieve specific entry by ID."""
        self._ensure_initialized()
        
        # Try vector DB first
        if self._collection is not None:
            try:
                results = self._collection.get(ids=[entry_id])
                if results and results["documents"]:
                    return ColdMemoryEntry.from_dict(results["metadatas"][0])
            except Exception as e:
                logger.debug(f"[ColdMemory] DB lookup failed: {e}")
        
        # Fallback to JSON
        json_file = self.backup_path / f"{entry_id}.json"
        if json_file.exists():
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return ColdMemoryEntry.from_dict(data)
            except Exception as e:
                logger.error(f"[ColdMemory] JSON read failed: {e}")
        
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about cold memory state."""
        total_entries = len(self._index)
        
        # Count by type
        type_counts = {}
        for meta in self._index.values():
            t = meta.get("type", "UNKNOWN")
            type_counts[t] = type_counts.get(t, 0) + 1
        
        db_size = 0
        if self.db_path.exists():
            db_size = sum(f.stat().st_size for f in self.db_path.rglob("*") if f.is_file())
        
        backup_size = 0
        if self.backup_path.exists():
            backup_size = sum(f.stat().st_size for f in self.backup_path.glob("*.json"))
        
        return {
            "total_entries": total_entries,
            "total_stored_ever": self._total_stored,
            "total_searches": self._total_searches,
            "type_distribution": type_counts,
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "backup_size_bytes": backup_size,
            "backup_size_mb": round(backup_size / (1024 * 1024), 2),
            "has_vector_db": self._collection is not None,
            "embedding_model": self.EMBEDDING_MODEL if self._embedding_model else "not loaded",
        }
    
    def cleanup_duplicates(self) -> int:
        """
        Remove duplicate entries based on content hashing.
        
        Returns:
            Number of duplicates removed
        """
        removed = 0
        seen_hashes = set()
        
        for content_hash in list(self._index.keys()):
            if content_hash in seen_hashes:
                entry_id = self._index[content_hash]["entry_id"]
                
                # Remove from vector DB
                if self._collection is not None:
                    try:
                        self._collection.delete(ids=[entry_id])
                    except Exception:
                        pass
                
                # Remove JSON backup
                json_file = self.backup_path / f"{entry_id}.json"
                if json_file.exists():
                    json_file.unlink()
                
                del self._index[content_hash]
                removed += 1
            else:
                seen_hashes.add(content_hash)
        
        if removed > 0:
            self._save_index()
            logger.info(f"[ColdMemory] Removed {removed} duplicate entries")
        
        return removed
    
    def export_all(self, output_path: str) -> bool:
        """
        Export all entries to a single JSON file.
        
        Useful for backups or migration.
        """
        self._ensure_initialized()
        
        all_entries = []
        
        # Collect from vector DB
        if self._collection is not None:
            try:
                results = self._collection.get(include=["documents", "metadatas"])
                
                for i, doc in enumerate(results["documents"]):
                    entry_data = results["metadatas"][i]
                    entry_data["full_content"] = doc
                    all_entries.append(entry_data)
                    
            except Exception as e:
                logger.error(f"[ColdMemory] Export failed: {e}")
                return False
        
        # Write to file
        try:
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output, 'w', encoding='utf-8') as f:
                json.dump({
                    "export_time": datetime.now().isoformat(),
                    "total_entries": len(all_entries),
                    "entries": all_entries
                }, f, ensure_ascii=False, indent=2)
            
            logger.info(f"[ColdMemory] Exported {len(all_entries)} entries to {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"[ColdMemory] Export write failed: {e}")
            return False
    
    @property
    def is_persistent(self) -> bool:
        """Cold memory persists to disk (and vector DB)."""
        return True
