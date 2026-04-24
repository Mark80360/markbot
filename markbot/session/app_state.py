"""App state provider with React-like API."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Generator, Optional, TypeVar

from markbot.session.types import AppState
from markbot.types.permission import PermissionMode, ToolPermissionContext
from markbot.session.store import StateStore

T = TypeVar("T")


class AppStateProvider:
    """
    Provider for application state.

    Provides a clean API for accessing and updating state.
    """

    _instance: Optional["AppStateProvider"] = None

    def __init__(self, store: Optional[StateStore] = None):
        self.store = store or StateStore()

    @classmethod
    def get_instance(cls) -> "AppStateProvider":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def initialize(cls, initial_state: Optional[AppState] = None) -> "AppStateProvider":
        """Initialize with state."""
        cls._instance = cls(StateStore(initial_state))
        return cls._instance

    # State access
    @property
    def state(self) -> AppState:
        return self.store.get()

    def get(self) -> AppState:
        return self.store.get()

    def set(self, updater: Callable[[AppState], AppState] | AppState) -> None:
        self.store.set(updater)

    def update(self, **kwargs: Any) -> None:
        """Update specific fields."""

        def updater(state: AppState) -> AppState:
            new_state = state.copy()
            for key, value in kwargs.items():
                if hasattr(new_state, key):
                    setattr(new_state, key, value)
            return new_state

        self.set(updater)

    # Permission helpers
    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Set permission mode."""

        def updater(state: AppState) -> AppState:
            new_state = state.copy()
            new_state.permission_mode = mode
            new_state.tool_permission_context = ToolPermissionContext(mode=mode)
            return new_state

        self.set(updater)

    def get_permission_mode(self) -> PermissionMode:
        return self.state.permission_mode

    # Session helpers
    def set_current_session(self, session: Any) -> None:
        self.update(current_session=session)

    def get_current_session(self) -> Optional[Any]:
        return self.state.current_session

    # Tool helpers
    def set_processing(self, is_processing: bool) -> None:
        self.update(is_processing=is_processing)

    def is_processing(self) -> bool:
        return self.state.is_processing

    # Context manager for batching updates
    @contextmanager
    def batch(
        self,
    ) -> Generator[list[Callable[[AppState], AppState]], None, None]:
        """Batch multiple updates into one notification."""
        updates: list[Callable[[AppState], AppState]] = []

        def queue(updater: Callable[[AppState], AppState]) -> None:
            updates.append(updater)

        yield queue

        if updates:

            def combined(state: AppState) -> AppState:
                new_state = state
                for update in updates:
                    new_state = update(new_state.copy())
                return new_state

            self.set(combined)


# Global accessor
def get_app_state() -> AppStateProvider:
    """Get the global app state provider."""
    return AppStateProvider.get_instance()
