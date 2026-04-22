"""State store with subscription support.

Inspired by Redux and MarkBot's AppState.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, Optional, TypeVar

from loguru import logger

from markbot.state.types import AppState
from markbot.bus.events import Event, EventType

T = TypeVar("T")


@dataclass
class StateSubscription(Generic[T]):
    """Subscription to state changes."""

    selector: Callable[[AppState], T]
    callback: Callable[[T, T], None]  # (new_value, old_value)
    last_value: Any = None


class StateStore:
    """
    Centralized state store with subscription support.

    Inspired by Redux and MarkBot's AppState.
    """

    def __init__(self, initial_state: Optional[AppState] = None):
        self._state = initial_state or AppState()
        self._subscriptions: list[StateSubscription] = []
        self._listeners: list[Callable[[AppState, AppState], None]] = []
        self._history: list[AppState] = []
        self._max_history = 50

    @property
    def state(self) -> AppState:
        """Get current state."""
        return self._state

    def get(self) -> AppState:
        """Get current state (alias)."""
        return self._state

    def set(self, updater: Callable[[AppState], AppState] | AppState) -> None:
        """
        Update state.

        Args:
            updater: New state or function that receives old state and returns new state
        """
        old_state = self._state

        if callable(updater):
            new_state = updater(old_state.copy())
        else:
            new_state = updater

        # Save to history
        self._history.append(old_state)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        self._state = new_state

        # Notify listeners
        self._notify(old_state, new_state)

    def _notify(self, old_state: AppState, new_state: AppState) -> None:
        """Notify all subscribers."""
        # Full state listeners
        for listener in self._listeners:
            try:
                listener(new_state, old_state)
            except Exception as e:
                logger.error(f"State listener error: {e}")

        # Selector-based subscriptions
        for sub in self._subscriptions:
            try:
                new_value = sub.selector(new_state)
                old_value = sub.selector(old_state)

                if new_value != old_value:
                    sub.callback(new_value, old_value)
                    sub.last_value = new_value

            except Exception as e:
                logger.error(f"Subscription error: {e}")

    def subscribe(
        self, callback: Callable[[AppState, AppState], None]
    ) -> Callable[[], None]:
        """
        Subscribe to all state changes.

        Returns unsubscribe function.
        """
        self._listeners.append(callback)

        def unsubscribe():
            self._listeners.remove(callback)

        return unsubscribe

    def select(
        self,
        selector: Callable[[AppState], T],
        callback: Callable[[T, T], None],
    ) -> Callable[[], None]:
        """
        Subscribe to a specific slice of state.

        Only calls callback when selected value changes.

        Returns unsubscribe function.
        """
        sub = StateSubscription(
            selector=selector,
            callback=callback,
            last_value=selector(self._state),
        )
        self._subscriptions.append(sub)

        def unsubscribe():
            self._subscriptions.remove(sub)

        return unsubscribe

    def undo(self) -> bool:
        """Undo last state change."""
        if not self._history:
            return False

        previous = self._history.pop()
        current = self._state
        self._state = previous
        self._notify(current, previous)
        return True
