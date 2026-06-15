"""Embedder — layered text-to-vector encoding for long-term memory.

The embedder is the heart of semantic recall: it turns text into dense
vectors so that ``memory_search("我喜欢用 Python 写后端")`` can find a
historical turn ``"我的技术栈是 Django + DRF"`` with zero lexical overlap.

Three backends, tried in priority order by :func:`build_embedder`:

1. **OpenAICompatibleEmbedder** — when ``api_key`` is set. Talks to the
   OpenAI ``/embeddings`` endpoint (or any compatible service via
   ``base_url``). Best quality, requires network + key.
2. **SentenceTransformerEmbedder** — when ``sentence_transformers`` is
   importable. Local CPU model, multilingual, ~470 MB download on first
   use. Good quality, fully offline after download.
3. **HashingEmbedder** — always available, zero dependencies. Character
   n-grams + CJK bigrams hashed into a fixed-dimensional sparse vector.
   Lowest recall quality but guarantees semantic search works in every
   environment.

All backends share the :class:`Embedder` interface: a ``dim`` property
and a batched ``embed(texts)`` method returning ``list[list[float]]``.

Dimension changes (e.g. switching backends) invalidate previously-stored
vectors — the :class:`LongTermMemory` layer detects this via
``embedding_sig`` and triggers a full reindex.
"""

from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from loguru import logger

# ---------------------------------------------------------------------------
# Embedder interface
# ---------------------------------------------------------------------------


class Embedder(ABC):
    """Abstract text-to-vector encoder.

    Implementations must be deterministic given the same model config:
    the same text always yields the same vector. They should be safe
    to call concurrently (the SQLite store relies on this).
    """

    #: Embedding dimensionality. Must be stable for the lifetime of the
    #: instance. Changing it invalidates all previously stored vectors.
    @property
    @abstractmethod
    def dim(self) -> int: ...

    #: Short backend identifier (e.g. ``"openai:text-embedding-3-small"``).
    #: Used to detect backend switches and trigger a reindex.
    @property
    @abstractmethod
    def backend_name(self) -> str: ...

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Encode a batch of texts into vectors.

        Args:
            texts: Non-empty sequence of strings.

        Returns:
            List of float vectors, one per input text, same order, each
            of length :attr:`dim`.
        """
        ...

    def embed_one(self, text: str) -> list[float]:
        """Convenience: encode a single string."""
        return self.embed([text])[0]

    @property
    def embedding_sig(self) -> str:
        """Stable signature combining backend + dimension.

        Two embedders with the same ``embedding_sig`` produce
        interchangeable vectors; a mismatch means stored vectors are
        incompatible and must be rebuilt.
        """
        return f"{self.backend_name}@d{self.dim}"


# ---------------------------------------------------------------------------
# Hashing embedder — zero-dependency fallback (always available)
# ---------------------------------------------------------------------------

# Basic CJK Unified Ideographs range.
_CJK_RANGE = "\u4e00-\u9fff"
# Match ASCII alphanumeric runs or single CJK chars.
_TOKEN_RE = re.compile(rf"[a-zA-Z0-9]+|[{_CJK_RANGE}]")

#: Dimension for the hashing embedder. 1024 is large enough to keep
#: collision rates negligible for memory-scale corpora (<1M entries)
#: while staying small enough to cosine-scan in memory quickly.
HASHING_DIM = 1024


def _tokens_with_ngrams(text: str) -> list[str]:
    """Tokenize for the hashing embedder.

    Produces character-level features: ASCII words, single CJK chars,
    CJK bigrams, and ASCII character 3-grams. This gives sub-word
    overlap so ``"running"`` and ``"runs"`` share a feature prefix.
    """
    if not text:
        return []
    lower = text.lower()
    tokens = _TOKEN_RE.findall(lower)

    feats: list[str] = []
    # 1. Whole tokens (ASCII words + CJK chars)
    feats.extend(tokens)
    # 2. CJK bigrams (captures "用户" / "记忆" as single features)
    for i in range(len(tokens) - 1):
        if tokens[i][0] >= "\u4e00" or tokens[i + 1][0] >= "\u4e00":
            feats.append(tokens[i] + tokens[i + 1])
    # 3. ASCII char 3-grams for sub-word overlap
    for tok in tokens:
        if len(tok) >= 3 and tok[0] < "\u4e00":
            for i in range(len(tok) - 2):
                feats.append("3g:" + tok[i : i + 3])
    return feats


class HashingEmbedder(Embedder):
    """Zero-dependency feature-hashing embedder.

    Maps text features (tokens, n-grams) into a fixed-dimensional
    vector via the hashing trick, with L2 normalization so cosine
    similarity is a plain dot product. No external model, no network,
    no install — always available as the last-resort fallback.
    """

    def __init__(self, dim: int = HASHING_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def backend_name(self) -> str:
        return f"hashing:{self._dim}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import math

        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            for feat in _tokens_with_ngrams(text):
                # Signed hashing: use two hash halves to produce a +/- sign,
                # which reduces cancellation and improves discriminative power.
                h = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
                bucket = int.from_bytes(h[:4], "little") % self._dim
                sign = 1.0 if (h[4] & 1) == 0 else -1.0
                vec[bucket] += sign
            # L2 normalize. A zero vector (no features) stays zero — the
            # cosine layer treats zero vectors as never-matching.
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                inv = 1.0 / norm
                vec = [v * inv for v in vec]
            out.append(vec)
        return out


# ---------------------------------------------------------------------------
# Sentence-Transformers embedder — local model (optional dependency)
# ---------------------------------------------------------------------------

#: Default multilingual model — good Chinese + English coverage in ~470 MB.
DEFAULT_ST_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


class SentenceTransformerEmbedder(Embedder):
    """Local embeddings via ``sentence-transformers``.

    Lazily loads the model on first ``embed()`` call so importing this
    module never triggers a download. If the package is missing or the
    model fails to load, :func:`build_embedder` falls through to the
    hashing embedder.
    """

    def __init__(self, model_name: str = DEFAULT_ST_MODEL) -> None:
        self._model_name = model_name
        self._model: Any = None
        self._dim_cache: int | None = None

    @property
    def backend_name(self) -> str:
        return f"sentence-transformers:{self._model_name}"

    @property
    def dim(self) -> int:
        if self._dim_cache is None:
            # Force model load to read the real dimension.
            self._ensure_model()
        assert self._dim_cache is not None
        return self._dim_cache

    def ensure_loaded(self) -> None:
        """Force-load the model if not yet loaded.

        Public entry point used by the factory to validate the backend
        at construction time. Idempotent.
        """
        self._ensure_model()

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is not installed; "
                "install with: pip install 'markbot[local-embeddings]' "
                "or pip install sentence-transformers"
            ) from exc
        logger.info("Loading sentence-transformers model '{}' (first use may download)...", self._model_name)
        self._model = SentenceTransformer(self._model_name)
        self._dim_cache = int(self._model.get_sentence_embedding_dimension())
        logger.info("Sentence-transformers ready (dim={})", self._dim_cache)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self._ensure_model()
        # sentence-transformers handles batching internally and returns
        # a numpy array of shape (n, dim). Convert to plain lists for
        # JSON/SQLite friendliness.
        import numpy as np  # local import; numpy is a sentence-transformers dep

        vecs = self._model.encode(list(texts), convert_to_numpy=True, show_progress_bar=False)
        if isinstance(vecs, np.ndarray):
            return [v.astype(float).tolist() for v in vecs]
        # Defensive: some backends may return lists already.
        return [list(v) for v in vecs]


# ---------------------------------------------------------------------------
# OpenAI-compatible embedder — API-backed (best quality, needs key)
# ---------------------------------------------------------------------------

#: Batch size for the OpenAI embeddings API (the endpoint caps input length).
_OPENAI_BATCH = 64


class OpenAICompatibleEmbedder(Embedder):
    """Embeddings via the OpenAI ``/embeddings`` API (or any compatible service).

    Uses ``httpx`` for synchronous calls — embeddings are produced on the
    write/search path where a brief blocking call is acceptable, and the
    caller (``LongTermMemory``) already offloads to a thread executor for
    the write path. The search path does a single short call.

    Configuration mirrors the existing ``embedding_*`` config fields:
    ``api_key``, ``base_url`` (empty = OpenAI default), ``model_name``.
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = "text-embedding-3-small",
        base_url: str = "",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAICompatibleEmbedder requires a non-empty api_key")
        self._api_key = api_key
        self._model_name = model_name or "text-embedding-3-small"
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._dim_cache: int | None = None

    @property
    def backend_name(self) -> str:
        return f"openai:{self._model_name}"

    @property
    def dim(self) -> int:
        if self._dim_cache is None:
            self._dim_cache = self._probe_dim()
        return self._dim_cache

    def _embeddings_url(self) -> str:
        if self._base_url:
            # Allow callers to pass either with or without /v1.
            if self._base_url.endswith("/embeddings"):
                return self._base_url
            return f"{self._base_url}/embeddings"
        return "https://api.openai.com/v1/embeddings"

    def _probe_dim(self) -> int:
        """Determine dimensionality with a single short probe call.

        Falls back to a model-known default table if the probe fails
        (e.g. transient network error) so we don't block startup.
        """
        known = {
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
            "text-embedding-ada-002": 1536,
        }
        try:
            vecs = self.embed(["dimension probe"])
            if vecs and vecs[0]:
                return len(vecs[0])
        except Exception as exc:
            logger.warning(
                "Embedding dimension probe failed ({}); using known default for '{}'",
                exc, self._model_name,
            )
        return known.get(self._model_name, 1536)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = self._embeddings_url()
        all_vecs: list[list[float]] = []

        # Batch to respect API input limits.
        for start in range(0, len(texts), _OPENAI_BATCH):
            batch = list(texts[start : start + _OPENAI_BATCH])
            payload = {"input": batch, "model": self._model_name}
            data = self._post_with_retry(httpx, headers, url, payload)
            arr = data.get("data", [])
            # Sort by index in case the API returns out of order.
            arr_sorted = sorted(arr, key=lambda d: d.get("index", 0))
            for item in arr_sorted:
                all_vecs.append([float(x) for x in item.get("embedding", [])])

        if len(all_vecs) != len(texts):
            raise RuntimeError(
                f"Embedding count mismatch: sent {len(texts)}, got {len(all_vecs)}"
            )
        return all_vecs

    def _post_with_retry(
        self,
        httpx_mod: Any,
        headers: dict[str, str],
        url: str,
        payload: dict[str, Any],
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """POST with exponential backoff on transient failures."""
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                with httpx_mod.Client(timeout=self._timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    # Retryable.
                    last_exc = RuntimeError(f"HTTP {resp.status_code}")
                    delay = min(2 ** attempt, 16)
                    logger.debug(
                        "Embedding API retryable status {} (attempt {}/{}), sleeping {}s",
                        resp.status_code, attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                delay = min(2 ** attempt, 16)
                logger.debug(
                    "Embedding API error {} (attempt {}/{}), sleeping {}s",
                    exc, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
        raise RuntimeError(f"Embedding API failed after {max_retries} retries: {last_exc}")


# ---------------------------------------------------------------------------
# Factory — layered auto-selection
# ---------------------------------------------------------------------------

#: Config keys read from the ``embedding_config`` dict (built in container.py).
#:   backend:   hint — "openai" | "ollama" | "local" | "auto" (default "auto")
#:   api_key:   API key for the OpenAI-compatible endpoint
#:   base_url:  endpoint base URL (empty = OpenAI default)
#:   model_name: model identifier


def _try_sentence_transformers(model_name: str) -> "SentenceTransformerEmbedder | None":
    """Attempt to construct *and load* a SentenceTransformerEmbedder.

    Returns ``None`` (rather than raising) when the package is missing
    or the model fails to load, so the factory can fall through to the
    hashing tier. We force-load the model here — at construction time —
    rather than on first ``embed()``, so a broken install is detected
    immediately and the fallback engages.
    """
    try:
        emb = SentenceTransformerEmbedder(model_name=model_name)
        emb.ensure_loaded()  # raises ImportError / RuntimeError on failure
        return emb
    except ImportError:
        logger.info("Embedder: sentence-transformers not installed, falling back")
        return None
    except Exception as exc:
        logger.warning("Embedder: sentence-transformers init failed ({}), falling back", exc)
        return None


def build_embedder(config: dict[str, Any] | None) -> Embedder:
    """Select the best available embedder given config + environment.

    Tries backends in priority order. Each tier logs whether it was
    selected or skipped and why. Always returns a working embedder —
    the :class:`HashingEmbedder` is the guaranteed fallback.

    Args:
        config: Embedding config dict. Recognized keys: ``backend``,
            ``api_key``, ``base_url``, ``model_name``.

    Returns:
        A ready-to-use :class:`Embedder` instance.
    """
    config = config or {}
    backend_hint = str(config.get("backend", "auto")).lower().strip()
    api_key = str(config.get("api_key", "") or "").strip()
    base_url = str(config.get("base_url", "") or "").strip()
    model_name = str(config.get("model_name", "") or "").strip()

    # --- Tier 1: OpenAI-compatible API ---------------------------------
    # "ollama" backend routes through the OpenAI-compatible path with a
    # local base_url; treat it the same way but require a non-empty key
    # (Ollama accepts any non-empty value, or the caller sets a real one).
    want_api = backend_hint in ("auto", "openai", "ollama")
    if want_api and api_key:
        try:
            # For Ollama, default the model and base_url if unset.
            if backend_hint == "ollama":
                if not base_url:
                    base_url = "http://localhost:11434/v1"
                if not model_name:
                    model_name = "nomic-embed-text"
            emb = OpenAICompatibleEmbedder(
                api_key=api_key,
                model_name=model_name or "text-embedding-3-small",
                base_url=base_url,
            )
            logger.info("Embedder: OpenAI-compatible API (model='{}')", emb._model_name)
            return emb
        except Exception as exc:
            logger.warning("OpenAI-compatible embedder unavailable: {}", exc)

    # --- Tier 2: local sentence-transformers ---------------------------
    want_local = backend_hint in ("auto", "local", "ollama")
    if want_local:
        emb = _try_sentence_transformers(model_name or DEFAULT_ST_MODEL)
        if emb is not None:
            logger.info("Embedder: sentence-transformers (model='{}')", emb._model_name)
            return emb

    # --- Tier 3: hashing (always works) --------------------------------
    logger.info("Embedder: hashing fallback (dim={})", HASHING_DIM)
    return HashingEmbedder()


__all__ = [
    "Embedder",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "OpenAICompatibleEmbedder",
    "build_embedder",
    "HASHING_DIM",
    "DEFAULT_ST_MODEL",
]
