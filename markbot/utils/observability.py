"""Observability integration — OpenTelemetry traces, metrics, and structured logging.

This module provides thin wrappers so the rest of markbot can emit
telemetry without importing OTel APIs directly.  When OpenTelemetry
packages are not installed, all operations are no-ops.

Usage::

    from markbot.utils.observability import get_tracer, get_meter

    tracer = get_tracer("markbot.agent")
    with tracer.start_as_current_span("agent_loop.iteration"):
        ...

    meter = get_meter("markbot")
    request_counter = meter.create_counter("llm.requests")
    request_counter.add(1, {"model": "claude-sonnet-4-20250514", "provider": "anthropic"})
"""

from __future__ import annotations

import contextvars
import time
import uuid
from contextlib import contextmanager
from typing import Any, Generator

from loguru import logger


_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


@contextmanager
def correlation_scope(cid: str | None = None) -> Generator[str, None, None]:
    token = _correlation_id.set(cid or uuid.uuid4().hex[:12])
    try:
        yield _correlation_id.get()
    finally:
        _correlation_id.reset(token)


try:
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource

    _OTELEMETRY_AVAILABLE = True
except ImportError:
    _OTELEMETRY_AVAILABLE = False


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, *args, **kwargs):
        pass

    def add_event(self, *args, **kwargs):
        pass

    def record_exception(self, *args, **kwargs):
        pass

    def is_recording(self) -> bool:
        return False

    @property
    def context(self):
        return None


class _NoopTracer:
    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()

    def start_span(self, name: str, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


class _NoopCounter:
    def add(self, amount: int | float, attributes: dict | None = None) -> None:
        pass


class _NoopHistogram:
    def record(self, amount: int | float, attributes: dict | None = None) -> None:
        pass


class _NoopMeter:
    def create_counter(self, name: str, **kwargs: Any) -> _NoopCounter:
        return _NoopCounter()

    def create_histogram(self, name: str, **kwargs: Any) -> _NoopHistogram:
        return _NoopHistogram()


_tracer: _NoopTracer | Any = _NoopTracer()
_meter: _NoopMeter | Any = _NoopMeter()
_initialized = False


def init_observability(
    service_name: str = "markbot",
    service_version: str = "1.0.0",
    otlp_endpoint: str | None = None,
    enable_metrics: bool = True,
) -> None:
    """Initialize OpenTelemetry SDK.  Safe to call multiple times."""
    global _tracer, _meter, _initialized
    if _initialized:
        return
    _initialized = True

    if not _OTELEMETRY_AVAILABLE:
        logger.info("[Observability] OpenTelemetry packages not installed — telemetry disabled")
        return

    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
    })

    try:
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)

        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(
                    __import__("opentelemetry.sdk.trace.export", fromlist=["BatchSpanProcessor"]).BatchSpanProcessor(
                        OTLPSpanExporter(endpoint=otlp_endpoint)
                    )
                )
                logger.info(f"[Observability] OTLP trace exporter configured → {otlp_endpoint}")
            except ImportError:
                logger.warning("[Observability] OTLP exporter not installed — traces stay local")

        _tracer = trace.get_tracer(service_name, service_version)

        if enable_metrics:
            try:
                mp = MeterProvider(resource=resource)
                metrics.set_meter_provider(mp)
                _meter = metrics.get_meter(service_name, service_version)
            except Exception as e:
                logger.warning(f"[Observability] Metrics init failed: {e}")

        logger.info("[Observability] OpenTelemetry initialized")
    except Exception as e:
        logger.warning(f"[Observability] Init failed, falling back to no-op: {e}")


def get_tracer(name: str = "markbot") -> Any:
    return _tracer


def get_meter(name: str = "markbot") -> Any:
    return _meter


@contextmanager
def span(name: str, **attributes: Any) -> Generator[Any, None, None]:
    """Convenience context manager for creating a traced span."""
    tr = get_tracer()
    with tr.start_as_current_span(name) as s:
        for k, v in attributes.items():
            s.set_attribute(k, v)
        s.set_attribute("correlation_id", get_correlation_id())
        yield s


def measure_latency(label: str):
    """Decorator / context manager factory for latency tracking."""
    class _LatencyTracker:
        def __init__(self):
            self.start = 0.0

        def __enter__(self):
            self.start = time.monotonic()
            return self

        def __exit__(self, *args):
            elapsed_ms = (time.monotonic() - self.start) * 1000
            hist = get_meter().create_histogram(f"markbot.{label}.duration_ms")
            hist.record(elapsed_ms)
            logger.debug(f"[Latency] {label}: {elapsed_ms:.1f}ms")

    return _LatencyTracker()


def structured_log(level: str, message: str, **kwargs: Any) -> None:
    """Emit a structured log entry with correlation_id and extra fields."""
    extra = {"correlation_id": get_correlation_id(), **kwargs}
    getattr(logger, level)(message, **extra)
