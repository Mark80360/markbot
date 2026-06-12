"""CLI / TUI cache chip renderer.

Renders the two most important cache signals as compact Rich spans:

  - **cache hit %** — the rate at which the most recent LLM response
    served input tokens from the server-side prefix cache.
  - **prefix stability** — the fraction of recent turns in which
    the system prompt + tool catalog bytes were unchanged.

Both signals are derived from :class:`markbot.agent.cache_protocol.CacheEvent`,
which is emitted by the prefix-stability manager on every LLM call.

## Colour rules

The colour thresholds (modelled on CodeWhale's footer chip):

  - **green** — hit_rate > 0.80  /  stability >= 0.95
  - **yellow** — 0.40 ≤ hit_rate ≤ 0.80  /  0.70 ≤ stability < 0.95
  - **red** — hit_rate < 0.40 (only when prefix is also suspect)  /
              stability < 0.70

When the provider did not report cache statistics, the hit_rate
chip is rendered as "Cache: unavailable" (not "0%") — see
:mod:`markbot.agent.cache_protocol` for the discipline behind this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rich.style import Style
from rich.text import Text

from markbot.agent.cache_protocol import CacheEvent

# ---------------------------------------------------------------------------
# Colour thresholds
# ---------------------------------------------------------------------------

HIT_GREEN = 0.80
HIT_YELLOW = 0.40
STABILITY_GREEN = 0.95
STABILITY_YELLOW = 0.70


@dataclass(frozen=True)
class ChipStyle:
    green: Style
    yellow: Style
    red: Style
    muted: Style


DEFAULT_STYLE = ChipStyle(
    green=Style(color="green", bold=True),
    yellow=Style(color="yellow", bold=True),
    red=Style(color="red", bold=True),
    muted=Style(color="bright_black"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hit_style(hit_rate: Optional[float], prefix_stability: float, palette: ChipStyle) -> Style:
    """Map (hit_rate, stability) to a colour.

    ``hit_rate`` of ``None`` means *unknown*; we render the chip as
    muted grey so the operator does not mistake it for "0%".
    """
    if hit_rate is None:
        return palette.muted
    if hit_rate > HIT_GREEN:
        return palette.green
    if hit_rate >= HIT_YELLOW:
        return palette.yellow
    # Only colour red when the prefix is also suspect.  A low hit
    # rate with a stable prefix is usually just "this turn is novel";
    # a low hit rate with an unstable prefix is "we keep invalidating
    # the cache, fix the prompt composition".
    if prefix_stability < STABILITY_YELLOW:
        return palette.red
    return palette.muted


def _stability_style(stability: float, palette: ChipStyle) -> Style:
    if stability >= STABILITY_GREEN:
        return palette.green
    if stability >= STABILITY_YELLOW:
        return palette.yellow
    return palette.red


def _format_pct(value: float) -> str:
    return f"{int(round(max(0.0, min(1.0, value)) * 100))}%"


# ---------------------------------------------------------------------------
# Public renderers
# ---------------------------------------------------------------------------

def render_cache_hit_chip(
    event: CacheEvent,
    palette: ChipStyle = DEFAULT_STYLE,
) -> Text:
    """Render a single ``Cache: 86%`` (or ``Cache: unavailable``) chip.

    The returned :class:`rich.text.Text` is suitable for dropping into
    a Rich ``Status`` bar or a TUI footer.
    """
    if event.cache_read_tokens is None or event.cache_miss_tokens is None:
        return Text("Cache: unavailable", style=palette.muted)
    total = event.cache_read_tokens + event.cache_miss_tokens
    if total <= 0:
        return Text("Cache: unavailable", style=palette.muted)
    rate = event.cache_read_tokens / total
    style = _hit_style(rate, event.stability_pct / 100.0, palette)
    label = f"Cache: {_format_pct(rate)}"
    return Text(label, style=style)


def render_prefix_stability_chip(
    event: CacheEvent,
    palette: ChipStyle = DEFAULT_STYLE,
) -> Text:
    """Render a ``Prefix: 100%`` (or ``Prefix: drift``) chip."""
    stability = event.stability_pct / 100.0
    style = _stability_style(stability, palette)
    if event.changed:
        # Use the drift label when this event represents a real change.
        return Text(f"Prefix: {event.label}", style=style)
    return Text(f"Prefix: {_format_pct(stability)}", style=style)


def render_cache_status_line(
    event: CacheEvent,
    palette: ChipStyle = DEFAULT_STYLE,
    separator: str = "  |  ",
) -> Text:
    """Render the combined "Cache: 86%  |  Prefix: 100%" line.

    Used by ``/status`` and the interactive prompt footer.
    """
    out = Text()
    out.append_text(render_cache_hit_chip(event, palette))
    out.append(separator, style=palette.muted)
    out.append_text(render_prefix_stability_chip(event, palette))
    if event.changed:
        out.append(separator, style=palette.muted)
        out.append(
            f"drift: {event.label} (hash={event.pinned_combined_hash})",
            style=palette.red,
        )
    return out


__all__ = [
    "ChipStyle",
    "DEFAULT_STYLE",
    "render_cache_hit_chip",
    "render_prefix_stability_chip",
    "render_cache_status_line",
]
