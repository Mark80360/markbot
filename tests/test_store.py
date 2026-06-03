"""Tests for markbot.session.store — StateStore with subscriptions."""


from markbot.session.store import StateStore, StateSubscription
from markbot.session.types import AppState
from markbot.types.permission import PermissionMode


class TestStateStore:
    def test_initial_state(self):
        store = StateStore()
        state = store.get()
        assert state.permission_mode == PermissionMode.DEFAULT
        assert state.is_processing is False
        assert state.theme == "default"

    def test_custom_initial_state(self):
        initial = AppState(theme="dark", verbose=True)
        store = StateStore(initial_state=initial)
        assert store.state.theme == "dark"
        assert store.state.verbose is True

    def test_get_returns_current_state(self):
        store = StateStore()
        assert store.get() is store.state

    def test_set_with_new_state(self):
        store = StateStore()
        new_state = AppState(theme="dark")
        store.set(new_state)
        assert store.state.theme == "dark"

    def test_set_with_updater_function(self):
        store = StateStore()
        store.set(lambda s: AppState(theme="light", verbose=True))
        assert store.state.theme == "light"
        assert store.state.verbose is True

    def test_set_preserves_old_state_in_history(self):
        store = StateStore()
        store.set(AppState(theme="dark"))
        store.set(AppState(theme="light"))
        assert len(store._history) == 2

    def test_undo(self):
        store = StateStore()
        store.set(AppState(theme="dark"))
        assert store.state.theme == "dark"
        result = store.undo()
        assert result is True
        assert store.state.theme == "default"

    def test_undo_empty_history(self):
        store = StateStore()
        result = store.undo()
        assert result is False

    def test_history_max_size(self):
        store = StateStore()
        store._max_history = 3
        for i in range(5):
            store.set(AppState(theme=f"theme_{i}"))
        assert len(store._history) == 3


class TestStateStoreSubscribe:
    def test_subscribe_notifies_on_change(self):
        store = StateStore()
        notifications = []

        def listener(new_state, old_state):
            notifications.append((new_state.theme, old_state.theme))

        store.subscribe(listener)
        store.set(AppState(theme="dark"))
        assert len(notifications) == 1
        assert notifications[0] == ("dark", "default")

    def test_unsubscribe(self):
        store = StateStore()
        notifications = []

        def listener(new_state, old_state):
            notifications.append(True)

        unsub = store.subscribe(listener)
        store.set(AppState(theme="dark"))
        assert len(notifications) == 1

        unsub()
        store.set(AppState(theme="light"))
        assert len(notifications) == 1  # no new notification

    def test_multiple_listeners(self):
        store = StateStore()
        calls = {"a": 0, "b": 0}

        store.subscribe(lambda n, o: calls.__setitem__("a", calls["a"] + 1))
        store.subscribe(lambda n, o: calls.__setitem__("b", calls["b"] + 1))
        store.set(AppState(theme="dark"))
        assert calls["a"] == 1
        assert calls["b"] == 1


class TestStateStoreSelect:
    def test_select_notifies_on_selected_change(self):
        store = StateStore()
        changes = []

        def on_theme_change(new_val, old_val):
            changes.append((new_val, old_val))

        store.select(lambda s: s.theme, on_theme_change)
        store.set(AppState(theme="dark"))
        assert len(changes) == 1
        assert changes[0] == ("dark", "default")

    def test_select_no_notification_on_unchanged(self):
        store = StateStore()
        changes = []

        def on_theme_change(new_val, old_val):
            changes.append((new_val, old_val))

        store.select(lambda s: s.theme, on_theme_change)
        store.set(AppState(verbose=True))  # theme unchanged
        assert len(changes) == 0

    def test_select_unsubscribe(self):
        store = StateStore()
        changes = []

        unsub = store.select(
            lambda s: s.theme,
            lambda n, o: changes.append(True),
        )
        store.set(AppState(theme="dark"))
        assert len(changes) == 1

        unsub()
        store.set(AppState(theme="light"))
        assert len(changes) == 1

    def test_select_initial_value(self):
        store = StateStore()
        sub = StateSubscription(
            selector=lambda s: s.theme,
            callback=lambda n, o: None,
            last_value=store.state.theme,
        )
        assert sub.last_value == "default"


class TestStateStoreListenerError:
    def test_listener_error_does_not_break(self):
        store = StateStore()
        calls = []

        def bad_listener(new_state, old_state):
            raise RuntimeError("boom")

        def good_listener(new_state, old_state):
            calls.append(True)

        store.subscribe(bad_listener)
        store.subscribe(good_listener)
        store.set(AppState(theme="dark"))
        assert len(calls) == 1  # good listener still called

    def test_select_callback_error_does_not_break(self):
        store = StateStore()
        store.select(
            lambda s: s.theme,
            lambda n, o: 1 / 0,  # raises ZeroDivisionError
        )
        # Should not raise
        store.set(AppState(theme="dark"))
