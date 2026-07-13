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
    #: Hard cap on records processed by the dedup pass.  The full pairwise
    #: similarity scan is O(n) in memory when blockwise (see _dedup_pass),
    #: but capping keeps wall-clock time bounded for very large stores.
    dedup_max_records: int = 10_000
    #: Row-block size for the blockwise similarity scan.  Each block
    #: allocates a (block x n) matrix; 512 x 50k x 4B = 100 MB — safe.
    dedup_block_size: int = 512


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
            # Best-effort forget of very low-importance turn noise before
            # promotion so the working set stays high-signal.
            forgotten = self._forget_low_importance_pass()
            if forgotten:
                logger.info("Consolidation forgot {} low-importance records", forgotten)
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
        """Merge near-duplicate records. Returns count removed.

        Uses a **blockwise** similarity scan so peak memory is
        O(block_size x n) rather than O(n^2).  A hard cap
        (dedup_max_records) bounds wall-clock time for very large
        stores; beyond the cap only the most-recently-accessed records
        are compared (the working set most likely to accumulate dups).
        """
        if self._store.count() < self.config.min_records_for_dedup:
            return 0
        records = self._store.iter_all()
        if len(records) < 2:
            return 0
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

        # Cap the working set to bound time.  Prefer frequently-accessed,
        # recent records because they are most likely to affect recall and
        # accumulate duplicate writes.
        cap = self.config.dedup_max_records
        if len(full) > cap:
            logger.info(
                "Consolidation dedup capped to {} of {} records",
                cap, len(full),
            )
            full.sort(key=lambda r: (r.access_count, r.created_at), reverse=True)
            full = full[:cap]
        if len(full) < 2:
            return 0

        mat = np.vstack([np.asarray(r.vector, dtype=np.float32) for r in full])
        mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)

        removed = 0
        survivors: set[int] = set(range(len(full)))
        block = self.config.dedup_block_size
        # Blockwise: for each row-block, compute sim against the FULL
        # matrix (block x n), then only examine pairs (i, j) with j in
        # the block and i < j.  Peak alloc is block x n, not n x n.
        for start in range(0, len(full), block):
            end = min(start + block, len(full))
            block_mat = mat[start:end]            # (b, dim)
            sims = block_mat @ mat.T              # (b, n)
            for bi in range(end - start):
                gi = start + bi                   # global index
                if gi not in survivors:
                    continue
                row = sims[bi]
                # Only look at j > gi to avoid double-counting pairs.
                for gj in range(gi + 1, len(full)):
                    if gj not in survivors:
                        continue
                    if row[gj] < self.config.dedup_threshold:
                        continue
                    ri, rj = full[gi], full[gj]
                    keep, drop = (gi, gj) if ri.access_count >= rj.access_count else (gj, gi)
                    if ri.access_count == rj.access_count:
                        keep, drop = (gi, gj) if ri.created_at >= rj.created_at else (gj, gi)
                    keeper = full[keep]
                    keeper.access_count += full[drop].access_count
                    try:
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

    # -- forget / decay -----------------------------------------------------

    def _forget_low_importance_pass(self) -> int:
        """Delete stale low-importance turn noise.

        Curated sources (memory/*, summary/*, user/profile) are never
        auto-forgotten here. Only rarely-accessed turn/delegation records
        older than one half-life can be removed.
        """
        now = time.time()
        half_life_s = max(self.config.age_decay_days, 1.0) * 86400.0
        removed = 0
        for rec in list(self._store.iter_all()):
            src = (rec.source or "").lower()
            if src.startswith(("memory/", "summary/", "user", "profile")):
                continue
            if not src.startswith(("turn/", "delegation/")):
                continue
            score = self.importance(rec, now=now)
            age_s = max(0.0, now - float(rec.created_at or 0.0))
            if score < 0.08 and age_s >= half_life_s and rec.access_count <= 1:
                try:
                    if self._store.delete(rec.id):
                        removed += 1
                except Exception as exc:
                    logger.debug("Consolidation forget failed: {}", exc)
        return removed

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
