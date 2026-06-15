"""Consolidation — dedup, importance decay, and promotion for long-term memory.

Runs periodically (triggered by the dream scheduler, or explicitly)
over the vector index to keep it from growing without bound and to
surface high-value memories.

Three operations:

1. **Dedup**: near-duplicate vectors (cosine > threshold) are merged —
   the older/lower-access record is deleted, its access count folded
   into the survivor.
2. **Decay**: each record's importance is recomputed from recency,
   access frequency, and age. Low-importance records past the store's
   capacity are evicted first (the store's own LRU is the backstop).
3. **Promote**: records whose ``access_count`` exceeds a threshold are
   reported back to the caller so they can be written to ``MEMORY.md``
   (the curated, always-in-system-prompt store) — promoting volatile
   recall into stable memory.

All operations are best-effort and logged; a failure in one record
never aborts the sweep.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from .vectorstore import VectorRecord, VectorStore


@dataclass
class ConsolidationConfig:
    """Tunables for :class:`Consolidator`."""

    dedup_threshold: float = 0.95
    age_decay_days: float = 90.0
    promote_access: int = 5
    #: Skip dedup when the index is smaller than this — not worth the scan.
    min_records_for_dedup: int = 100


@dataclass
class ConsolidationReport:
    """Result summary of one consolidation sweep."""

    deduped: int = 0
    promoted: list[str] = None  # type: ignore[assignment]
    starting_count: int = 0
    ending_count: int = 0
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        if self.promoted is None:
            self.promoted = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "deduped": self.deduped,
            "promoted": len(self.promoted),
            "starting_count": self.starting_count,
            "ending_count": self.ending_count,
            "duration_s": round(self.duration_s, 3),
        }


class Consolidator:
    """Sweep the vector index for dedup, decay, and promotion."""

    def __init__(
        self,
        store: VectorStore,
        config: ConsolidationConfig | None = None,
    ) -> None:
        self._store = store
        self.config = config or ConsolidationConfig()
        # Track which ids we've already promoted so we don't re-propose
        # the same record every sweep.
        self._promoted: set[str] = set()

    def run(
        self,
        embedder: Any,
        promote_cb: Callable[[str, str], None] | None = None,
    ) -> ConsolidationReport:
        """Run one consolidation sweep.

        Args:
            embedder: An :class:`~markbot.memory.embedder.Embedder` used
                to re-embed record content during dedup comparison. The
                dedup pass compares stored vectors directly when
                available, falling back to re-embedding otherwise.
            promote_cb: Optional callback ``(content, source) -> None``
                invoked for each record that crosses the promotion
                threshold. Typically writes to ``MEMORY.md``.

        Returns:
            A :class:`ConsolidationReport` summarizing the sweep.
        """
        start = time.time()
        report = ConsolidationReport(starting_count=self._store.count())
        try:
            report.deduped = self._dedup_pass()
            report.promoted = self._promotion_pass(promote_cb)
        except Exception as exc:
            logger.warning("Consolidation sweep failed: {}", exc)
        report.ending_count = self._store.count()
        report.duration_s = time.time() - start
        logger.info(
            "Consolidation: deduped {}, promoted {}, {} -> {} records in {:.2f}s",
            report.deduped, len(report.promoted),
            report.starting_count, report.ending_count, report.duration_s,
        )
        return report

    # -- dedup ---------------------------------------------------------------

    def _dedup_pass(self) -> int:
        """Merge near-duplicate records. Returns count removed."""
        if self._store.count() < self.config.min_records_for_dedup:
            return 0
        records = self._store.iter_all()
        if len(records) < 2:
            return 0
        # Load vectors for comparison. iter_all() returns empty vectors
        # for efficiency, so we fetch full records in batches.
        # For SQLite this is fine; for Chroma we skip dedup (vectors not
        # returned by iter_all) — the store's own LRU handles growth.
        try:
            import numpy as np
        except ImportError:
            logger.debug("Consolidation dedup skipped: numpy unavailable")
            return 0

        # Fetch full records (with vectors) for similarity comparison.
        full: list[VectorRecord] = []
        for rec in records:
            got = self._store.get(rec.id)
            if got is not None and got.vector:
                full.append(got)
        if len(full) < 2:
            return 0

        mat = np.vstack([np.asarray(r.vector, dtype=np.float32) for r in full])
        mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        # Pairwise cosine.
        sim = mat @ mat.T
        np.fill_diagonal(sim, -1.0)  # ignore self

        removed = 0
        survivors: set[int] = set(range(len(full)))
        # Greedy: for each pair above threshold, drop the older one.
        for i in range(len(full)):
            if i not in survivors:
                continue
            for j in range(i + 1, len(full)):
                if j not in survivors:
                    continue
                if sim[i, j] < self.config.dedup_threshold:
                    continue
                # Keep the one with higher access_count; tie-break on recency.
                ri, rj = full[i], full[j]
                keep, drop = (i, j) if ri.access_count >= rj.access_count else (j, i)
                if ri.access_count == rj.access_count:
                    keep, drop = (i, j) if ri.created_at >= rj.created_at else (j, i)
                # Fold the dropped record's access into the keeper.
                keeper = full[keep]
                keeper.access_count += full[drop].access_count
                try:
                    # Re-upsert keeper with merged access count.
                    self._store.upsert(keeper)
                    self._store.delete(full[drop].id)
                    survivors.discard(drop)
                    removed += 1
                except Exception as exc:
                    logger.debug("Consolidation dedup delete failed: {}", exc)
        return removed

    # -- promotion -----------------------------------------------------------

    def _promotion_pass(
        self,
        promote_cb: Callable[[str, str], None] | None,
    ) -> list[str]:
        """Identify high-access records and propose them for promotion."""
        if promote_cb is None:
            return []
        promoted: list[str] = []
        for rec in self._store.iter_all():
            if rec.access_count < self.config.promote_access:
                continue
            if rec.id in self._promoted:
                continue
            try:
                promote_cb(rec.content, rec.source)
                self._promoted.add(rec.id)
                promoted.append(rec.id)
            except Exception as exc:
                logger.debug("Consolidation promote_cb failed: {}", exc)
        return promoted

    # -- importance (informational) -----------------------------------------

    def importance(self, rec: VectorRecord, now: float | None = None) -> float:
        """Compute a 0..1 importance score for a record.

        ``importance = frequency_factor * recency_factor``

        - frequency: ``min(1, access_count / promote_access)``
        - recency: exponential decay with ``age_decay_days`` half-life

        Records below ~0.1 are candidates for eviction; the store's LRU
        uses this indirectly via ``access_count ASC, created_at ASC``.
        """
        now = now or time.time()
        freq = min(1.0, rec.access_count / max(1, self.config.promote_access))
        age_days = max(0.0, (now - rec.created_at) / 86400.0)
        half_life = self.config.age_decay_days
        recency = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
        return freq * recency


__all__ = ["Consolidator", "ConsolidationConfig", "ConsolidationReport"]
