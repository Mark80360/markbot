"""Outcome gates — ensure the agent finishes work that is verifiable.

Lightweight completion policy used by IterationRunner before accepting a
final response. Complements verify-on-stop nudges with a single decision
object (allow / nudge / force_footer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class OutcomeAction(str, Enum):
    ALLOW = "allow"
    NUDGE = "nudge"
    FOOTER = "footer"


@dataclass(frozen=True)
class OutcomeDecision:
    action: OutcomeAction = OutcomeAction.ALLOW
    reason: str = ""
    message: str = ""


@dataclass
class OutcomeGateConfig:
    enabled: bool = True
    max_nudges: int = 2
    surfaces: frozenset[str] = field(
        default_factory=lambda: frozenset({"cli", "web"})
    )
    require_verification_for_mutations: bool = True
    require_verification_for_side_effects: bool = True

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "OutcomeGateConfig":
        if settings is None:
            return cls()
        if isinstance(settings, Mapping):
            get = settings.get
        else:
            def get(k, d=None):
                return getattr(settings, k, d)

        surfaces = get("surfaces", None)
        if surfaces is None:
            surf = frozenset({"cli", "web"})
        else:
            surf = frozenset(str(s) for s in surfaces)
        return cls(
            enabled=bool(get("enabled", True)),
            max_nudges=int(get("max_nudges", 2)),
            surfaces=surf,
            require_verification_for_mutations=bool(
                get("require_verification_for_mutations", True)
            ),
            require_verification_for_side_effects=bool(
                get("require_verification_for_side_effects", True)
            ),
        )


@dataclass
class OutcomeGate:
    """Decide whether a final response is acceptable given side effects."""

    config: OutcomeGateConfig = field(default_factory=OutcomeGateConfig)

    def evaluate(
        self,
        *,
        surface: str,
        file_mutations: Sequence[Any],
        verification_done: bool,
        side_effect_pending: bool,
        nudge_count: int,
        claims_completion: bool = True,
    ) -> OutcomeDecision:
        cfg = self.config
        if not cfg.enabled:
            return OutcomeDecision(action=OutcomeAction.ALLOW)
        if surface not in cfg.surfaces:
            return OutcomeDecision(action=OutcomeAction.ALLOW)

        needs_verify = False
        reasons: list[str] = []
        if (
            cfg.require_verification_for_mutations
            and file_mutations
            and not verification_done
        ):
            needs_verify = True
            reasons.append("file mutations without verification")
        if cfg.require_verification_for_side_effects and side_effect_pending:
            needs_verify = True
            reasons.append("pending side effects without verification")

        if not needs_verify:
            return OutcomeDecision(action=OutcomeAction.ALLOW)

        reason = "; ".join(reasons)
        if nudge_count < cfg.max_nudges and claims_completion:
            return OutcomeDecision(
                action=OutcomeAction.NUDGE,
                reason=reason,
                message=(
                    "[Outcome Gate] You claimed the task is done, but "
                    f"{reason}. Run a verification step (tests, read-back, "
                    "or status check) before finalizing — or explain why "
                    "verification is impossible."
                ),
            )
        return OutcomeDecision(
            action=OutcomeAction.FOOTER,
            reason=reason,
            message=(
                "\n\n[Outcome Gate] Completion claimed without verification "
                f"({reason}). Treat residual risk as open."
            ),
        )
