"""Async message bus for decoupled channel-agent communication.

Enhanced with priority queuing, back-pressure, and per-session partitioning.

Backward-compatible: the original ``publish_inbound`` / ``consume_inbound``
API still works.  New features are opt-in via keyword arguments.
"""

from __future__ import annotations

import asyncio
import enum
from collections import defaultdict
from typing import Any

from loguru import logger

from markbot.bus.events import InboundMessage, OutboundMessage


class Priority(enum.IntEnum):
    SYSTEM = 0
    HEARTBEAT = 1
    USER = 2


class BackpressurePolicy(enum.Enum):
    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"
    REJECT = "reject"


class QueueFullError(Exception):
    def __init__(self, direction: str, size: int) -> None:
        self.direction = direction
        self.size = size
        super().__init__(f"{direction} queue full (size={size})")


class _PriorityQueue:
    """Heap-backed async priority queue.

    Lower ``Priority`` value = higher priority = consumed first.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._queues: dict[Priority, asyncio.Queue] = {
            p: asyncio.Queue(maxsize=maxsize) for p in Priority
        }
        self._event = asyncio.Event()
        self._total = 0

    def _signal(self) -> None:
        if self._total > 0:
            self._event.set()
        else:
            self._event.clear()

    async def put(self, item: Any, priority: Priority = Priority.USER) -> None:
        await self._queues[priority].put(item)
        self._total += 1
        self._signal()

    def put_nowait(self, item: Any, priority: Priority = Priority.USER) -> None:
        self._queues[priority].put_nowait(item)
        self._total += 1
        self._signal()

    def put_with_backpressure(
        self,
        item: Any,
        priority: Priority = Priority.USER,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
    ) -> bool:
        """Non-blocking put with back-pressure policy. Returns True on success."""
        q = self._queues[priority]
        if not q.full():
            q.put_nowait(item)
            self._total += 1
            self._signal()
            return True

        if policy == BackpressurePolicy.DROP_OLDEST:
            try:
                q.get_nowait()
                self._total -= 1
            except asyncio.QueueEmpty:
                pass
            q.put_nowait(item)
            self._total += 1
            self._signal()
            return True

        if policy == BackpressurePolicy.REJECT:
            return False

        return False

    async def get(self) -> Any:
        while True:
            while self._total == 0:
                self._event.clear()
                await self._event.wait()

            for p in sorted(Priority):
                q = self._queues[p]
                try:
                    item = q.get_nowait()
                    self._total -= 1
                    self._signal()
                    return item
                except asyncio.QueueEmpty:
                    continue

            self._event.clear()
            await self._event.wait()

    @property
    def qsize(self) -> int:
        return self._total

    @property
    def is_full(self) -> bool:
        return self._total >= self._maxsize

    @property
    def usage_ratio(self) -> float:
        if self._maxsize <= 0:
            return 0.0
        return self._total / self._maxsize


class _PartitionedQueue:
    """Per-session-key FIFO queue with a global consumption order.

    Messages are partitioned by ``session_key`` so that one slow session
    cannot starve others.  ``get()`` returns the oldest message across
    all partitions (fair round-robin).
    """

    def __init__(self, maxsize_per_partition: int = 200) -> None:
        self._maxsize = maxsize_per_partition
        self._partitions: dict[str, asyncio.Queue] = defaultdict(
            lambda: asyncio.Queue(maxsize=maxsize_per_partition)
        )
        self._global_event = asyncio.Event()
        self._total = 0
        self._round_robin_keys: list[str] = []
        self._rr_index = 0

    async def put(self, key: str, item: Any) -> None:
        await self._partitions[key].put(item)
        self._total += 1
        self._global_event.set()

    def put_nowait(self, key: str, item: Any) -> None:
        self._partitions[key].put_nowait(item)
        self._total += 1
        self._global_event.set()

    async def get(self) -> tuple[str, Any]:
        while self._total == 0:
            self._global_event.clear()
            await self._global_event.wait()

        keys = list(self._partitions.keys())
        start = self._rr_index % len(keys) if keys else 0
        for i in range(len(keys)):
            k = keys[(start + i) % len(keys)]
            q = self._partitions[k]
            try:
                item = q.get_nowait()
                self._total -= 1
                self._rr_index = (start + i + 1) % len(keys)
                if self._total == 0:
                    self._global_event.clear()
                return k, item
            except asyncio.QueueEmpty:
                continue

        self._global_event.clear()
        await self._global_event.wait()
        return await self.get()

    @property
    def qsize(self) -> int:
        return self._total


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.

    Enhanced features (backward-compatible):
    - **Priority queuing**: system messages consumed before user messages.
    - **Back-pressure**: configurable policy when queues are full.
    - **Per-session partitioning**: slow sessions don't block others.
    """

    _DEFAULT_MAX_QUEUE_SIZE = 1000

    def __init__(
        self,
        maxsize: int = 0,
        *,
        enable_priority: bool = False,
        enable_partitioning: bool = False,
        backpressure: BackpressurePolicy = BackpressurePolicy.BLOCK,
    ) -> None:
        _max = maxsize or self._DEFAULT_MAX_QUEUE_SIZE
        self._maxsize = _max
        self._enable_priority = enable_priority
        self._enable_partitioning = enable_partitioning
        self._backpressure = backpressure

        if enable_priority:
            self._inbound_pq = _PriorityQueue(maxsize=_max)
            self._outbound_pq = _PriorityQueue(maxsize=_max)
        elif enable_partitioning:
            self._inbound_part = _PartitionedQueue(maxsize_per_partition=200)
            self._outbound_part = _PartitionedQueue(maxsize_per_partition=200)
        else:
            self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=_max)
            self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=_max)

        self._stats = {"inbound_total": 0, "outbound_total": 0, "inbound_dropped": 0, "outbound_dropped": 0}

    async def publish_inbound(self, msg: InboundMessage, *, priority: Priority = Priority.USER) -> None:
        """Publish a message from a channel to the agent."""
        self._stats["inbound_total"] += 1

        if self._enable_priority:
            await self._inbound_pq.put(msg, priority)
        elif self._enable_partitioning:
            await self._inbound_part.put(msg.session_key, msg)
        else:
            await self.inbound.put(msg)

    def publish_inbound_nowait(
        self,
        msg: InboundMessage,
        *,
        priority: Priority = Priority.USER,
    ) -> bool:
        """Non-blocking publish with back-pressure. Returns True on success."""
        self._stats["inbound_total"] += 1

        try:
            if self._enable_priority:
                return self._inbound_pq.put_with_backpressure(msg, priority, self._backpressure)
            elif self._enable_partitioning:
                self._inbound_part.put_nowait(msg.session_key, msg)
                return True
            else:
                self.inbound.put_nowait(msg)
                return True
        except asyncio.QueueFull:
            self._stats["inbound_dropped"] += 1
            if self._backpressure == BackpressurePolicy.DROP_OLDEST:
                try:
                    self.inbound.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.inbound.put_nowait(msg)
                return True
            return False

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        if self._enable_priority:
            return await self._inbound_pq.get()
        elif self._enable_partitioning:
            _, msg = await self._inbound_part.get()
            return msg
        else:
            return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage, *, priority: Priority = Priority.USER) -> None:
        """Publish a response from the agent to channels."""
        self._stats["outbound_total"] += 1

        if self._enable_priority:
            await self._outbound_pq.put(msg, priority)
        elif self._enable_partitioning:
            await self._outbound_part.put(msg.channel, msg)
        else:
            await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        if self._enable_priority:
            return await self._outbound_pq.get()
        elif self._enable_partitioning:
            _, msg = await self._outbound_part.get()
            return msg
        else:
            return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        if self._enable_priority:
            return self._inbound_pq.qsize
        elif self._enable_partitioning:
            return self._inbound_part.qsize
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        if self._enable_priority:
            return self._outbound_pq.qsize
        elif self._enable_partitioning:
            return self._outbound_part.qsize
        return self.outbound.qsize()

    @property
    def is_inbound_full(self) -> bool:
        if self._enable_priority:
            return self._inbound_pq.is_full
        return self.inbound.full()

    @property
    def inbound_usage_ratio(self) -> float:
        if self._enable_priority:
            return self._inbound_pq.usage_ratio
        if self._maxsize <= 0:
            return 0.0
        return self.inbound.qsize() / self._maxsize

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)
