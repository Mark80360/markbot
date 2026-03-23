"""Local knowledge search package."""

from markbot.search.indexer import Indexer
from markbot.search.store import DocumentResult, SearchResult, SearchStore

__all__ = [
    "SearchStore",
    "SearchResult",
    "DocumentResult",
    "Indexer",
]


def __getattr__(name: str):
    """Lazy-export optional embedder to avoid eager optional-dependency exposure."""
    if name == "SentenceTransformerEmbedder":
        from markbot.search.embedder import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder
    raise AttributeError(f"module 'markbot.search' has no attribute {name!r}")
