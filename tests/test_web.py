"""Tests for markbot.web package (auth, store, server, routers)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from markbot.web import auth
from markbot.web.store import WebSessionStore


# ---------------------------------------------------------------------------
# auth.py
# ---------------------------------------------------------------------------


class TestAuthToken:
    def test_get_token_returns_nonempty_string(self):
        assert isinstance(auth.get_token(), str)
        assert len(auth.get_token()) > 0

    def test_regenerate_token_changes_value(self, monkeypatch):
        original = auth.get_token()
        monkeypatch.setattr(auth, "_session_token", original)
        new = auth.regenerate_token()
        assert new != original
        assert auth.get_token() == new

    def test_get_token_reflects_module_state(self, monkeypatch):
        monkeypatch.setattr(auth, "_session_token", "fixed-token-123")
        assert auth.get_token() == "fixed-token-123"


class TestAuthIsExempt:
    def _middleware(self):
        return auth.TokenAuthMiddleware(app=FastAPI())

    def test_non_api_path_is_exempt(self):
        m = self._middleware()
        assert m._is_exempt("/") is True
        assert m._is_exempt("/assets/index.js") is True
        assert m._is_exempt("/favicon.ico") is True

    def test_api_status_is_exempt(self):
        m = self._middleware()
        assert m._is_exempt("/api/status") is True

    def test_other_api_paths_require_auth(self):
        m = self._middleware()
        assert m._is_exempt("/api/sessions") is False
        assert m._is_exempt("/api/system/stats") is False


def _build_authed_app(token: str, include_router=None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(auth.TokenAuthMiddleware)

    @app.get("/api/echo")
    async def echo():
        return {"ok": True}

    if include_router is not None:
        app.include_router(include_router)
    return app


class TestAuthMiddleware:
    def test_unauthorized_without_token(self, monkeypatch):
        monkeypatch.setattr(auth, "_session_token", "secret")
        client = TestClient(_build_authed_app("secret"))
        resp = client.get("/api/echo")
        assert resp.status_code == 401
        assert resp.json() == {"error": "Unauthorized"}

    def test_unauthorized_with_wrong_token(self, monkeypatch):
        monkeypatch.setattr(auth, "_session_token", "secret")
        client = TestClient(_build_authed_app("secret"))
        resp = client.get("/api/echo", headers={"x-markbot-session-token": "wrong"})
        assert resp.status_code == 401

    def test_authorized_with_correct_token(self, monkeypatch):
        monkeypatch.setattr(auth, "_session_token", "secret")
        client = TestClient(_build_authed_app("secret"))
        resp = client.get("/api/echo", headers={"x-markbot-session-token": "secret"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_exempt_status_path_does_not_require_token(self, monkeypatch):
        from markbot.web.routers.status import router as status_router

        monkeypatch.setattr(auth, "_session_token", "secret")
        app = _build_authed_app("secret", include_router=status_router)
        client = TestClient(app)
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "version" in body
        assert "token" not in body


class TestVerifyWsToken:
    def test_valid_token(self, monkeypatch):
        monkeypatch.setattr(auth, "_session_token", "ws-secret")

        class FakeWS:
            def __init__(self, token: str | None):
                self._q = {"token": token} if token is not None else {}

            @property
            def query_params(self):
                return self._q

        import asyncio

        result = asyncio.run(auth.verify_ws_token(FakeWS("ws-secret")))
        assert result is True

    def test_invalid_token(self, monkeypatch):
        monkeypatch.setattr(auth, "_session_token", "ws-secret")

        class FakeWS:
            @property
            def query_params(self):
                return {"token": "nope"}

        import asyncio

        result = asyncio.run(auth.verify_ws_token(FakeWS()))
        assert result is False

    def test_missing_token(self, monkeypatch):
        monkeypatch.setattr(auth, "_session_token", "ws-secret")

        class FakeWS:
            @property
            def query_params(self):
                return {}

        import asyncio

        result = asyncio.run(auth.verify_ws_token(FakeWS()))
        assert result is False


# ---------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> WebSessionStore:
    return WebSessionStore(db_path=tmp_path / "test_web.db")


class TestWebSessionStore:
    def test_create_and_get_session(self, store):
        s = store.create_session("s1", "Hello")
        assert s["id"] == "s1"
        assert s["title"] == "Hello"
        assert s["message_count"] == 0
        got = store.get_session("s1")
        assert got is not None
        assert got["id"] == "s1"
        assert got["messages"] == []

    def test_get_missing_session_returns_none(self, store):
        assert store.get_session("nope") is None

    def test_list_sessions_ordered_by_last_active(self, store):
        store.create_session("a", "A")
        store.add_message("a", "user", "hi")
        store.create_session("b", "B")
        listed = store.list_sessions()
        ids = [s["id"] for s in listed]
        # b was created last → has greater last_active
        assert ids[0] == "b"

    def test_list_sessions_pagination(self, store):
        for i in range(5):
            store.create_session(f"s{i}", f"title-{i}")
        page = store.list_sessions(limit=2, offset=0)
        assert len(page) == 2
        page2 = store.list_sessions(limit=2, offset=2)
        assert len(page2) == 2
        # no overlap
        assert {p["id"] for p in page}.isdisjoint({p["id"] for p in page2})

    def test_delete_session(self, store):
        store.create_session("s1", "t")
        assert store.delete_session("s1") is True
        assert store.get_session("s1") is None
        assert store.delete_session("s1") is False

    def test_update_title(self, store):
        store.create_session("s1", "old")
        assert store.update_title("s1", "new") is True
        assert store.get_session("s1")["title"] == "new"
        assert store.update_title("missing", "x") is False

    def test_add_message_increments_count(self, store):
        store.create_session("s1", "t")
        mid = store.add_message("s1", "user", "hello")
        assert mid > 0
        got = store.get_session("s1")
        assert got["message_count"] == 1
        assert len(got["messages"]) == 1
        assert got["messages"][0]["role"] == "user"
        assert got["messages"][0]["content"] == "hello"

    def test_add_message_with_metadata(self, store):
        store.create_session("s1", "t")
        store.add_message("s1", "assistant", "hi", {"foo": "bar"})
        got = store.get_session("s1")
        assert got["messages"][0]["metadata"] == {"foo": "bar"}

    def test_add_messages_batch(self, store):
        store.create_session("s1", "t")
        store.add_messages_batch("s1", [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ])
        got = store.get_session("s1")
        assert got["message_count"] == 2
        assert [m["content"] for m in got["messages"]] == ["a", "b"]

    def test_delete_messages_from(self, store):
        store.create_session("s1", "t")
        store.add_message("s1", "user", "first")
        ts = store.get_session("s1")["messages"][0]["timestamp"]
        store.add_message("s1", "assistant", "second")
        deleted = store.delete_messages_from("s1", ts)
        assert deleted == 2  # both >= ts (ts is per-message-now, may match both)
        got = store.get_session("s1")
        # remaining message_count reflects actual rows
        assert got["message_count"] == len(got["messages"])
        assert got["message_count"] <= 1

    def test_delete_empty_sessions(self, store):
        store.create_session("empty1", "t")
        store.create_session("filled", "t")
        store.add_message("filled", "user", "x")
        count = store.delete_empty_sessions()
        assert count == 1
        assert store.get_session("empty1") is None
        assert store.get_session("filled") is not None

    def test_get_session_stats(self, store):
        store.create_session("a", "t")
        store.create_session("b", "t")
        store.add_message("b", "user", "x")
        stats = store.get_session_stats()
        assert stats["total"] == 2
        assert stats["active"] == 1
        assert stats["messages"] == 1

    def test_bulk_delete_sessions(self, store):
        for i in range(3):
            store.create_session(f"s{i}", "t")
        n = store.bulk_delete_sessions(["s0", "s1"])
        assert n == 2
        assert store.list_sessions() == [{"id": "s2"}] or len(store.list_sessions()) == 1
        assert store.bulk_delete_sessions([]) == 0

    def test_export_session_markdown(self, store):
        store.create_session("s1", "My Chat")
        store.add_message("s1", "user", "Hello")
        store.add_message("s1", "assistant", "World")
        md = store.export_session_markdown("s1")
        assert md is not None
        assert "# My Chat" in md
        assert "Hello" in md
        assert "World" in md
        assert "You" in md
        assert "Markbot" in md

    def test_export_missing_session_returns_none(self, store):
        assert store.export_session_markdown("nope") is None

    def test_search_sessions_empty_query_lists(self, store):
        store.create_session("s1", "t")
        results = store.search_sessions("")
        assert len(results) == 1
        assert results[0]["id"] == "s1"

    def test_search_sessions_with_query(self, store):
        store.create_session("s1", "alpha")
        store.add_message("s1", "user", "findme term")
        store.create_session("s2", "beta")
        store.add_message("s2", "user", "other")
        results = store.search_sessions("findme")
        ids = {r["id"] for r in results}
        assert "s1" in ids
        assert "s2" not in ids

    def test_db_path_parent_created(self, tmp_path):
        nested = tmp_path / "nested" / "deep" / "web.db"
        s = WebSessionStore(db_path=nested)
        assert nested.parent.exists()

    def test_fts_table_present(self, store):
        # Verify the FTS virtual table was created (search depends on it)
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='web_messages_fts'"
        ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# server.py — app factory smoke test (mock heavy agent creation)
# ---------------------------------------------------------------------------


class TestServerBuildApp:
    def test_build_app_returns_fastapi_instance(self, monkeypatch):
        # Avoid actually creating the agent loop / cron at import time;
        # _build_app only instantiates agent lazily on ws/upload, so it's safe.
        from markbot.web.server import _build_app

        app = _build_app()
        from fastapi import FastAPI

        assert isinstance(app, FastAPI)
        # status router registered and exempt
        routes = [getattr(r, "path", "") for r in app.routes]
        assert "/api/status" in routes

    def test_build_app_registers_routers(self, monkeypatch):
        from markbot.web.server import _build_app

        app = _build_app()
        routes = {getattr(r, "path", "") for r in app.routes}
        # A representative subset of registered endpoints
        for path in [
            "/api/status",
            "/api/sessions",
            "/api/sessions/stats",
            "/api/system/stats",
            "/api/config",
            "/api/env",
            "/api/logs",
        ]:
            assert path in routes

    def test_workspace_override_controls_upload_dir(self, tmp_path, monkeypatch):
        from markbot.web import auth
        from markbot.web import server
        from markbot.config.schema import Config

        monkeypatch.setattr(auth, "_session_token", "tok")
        monkeypatch.setattr(server, "WEB_DIST", tmp_path / "missing-dist")
        monkeypatch.setattr("markbot.config.loader.load_config", lambda: Config())

        workspace = tmp_path / "workspace"
        app = server._build_app(workspace=workspace)
        client = TestClient(app)
        resp = client.post(
            "/api/upload",
            files={"file": ("note.txt", b"hello", "text/plain")},
            headers={"x-markbot-session-token": "tok"},
        )

        assert resp.status_code == 200
        upload_name = resp.json()["name"]
        assert (workspace / ".web_uploads" / upload_name).exists()


# ---------------------------------------------------------------------------
# routers/status.py
# ---------------------------------------------------------------------------


class TestStatusRouter:
    def test_status_returns_version_without_token(self, monkeypatch):
        from markbot.web.routers.status import router as status_router

        monkeypatch.setattr(auth, "_session_token", "abc")
        app = FastAPI()
        app.include_router(status_router)
        client = TestClient(app)
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "version" in body
        assert "token" not in body


# ---------------------------------------------------------------------------
# routers/system.py
# ---------------------------------------------------------------------------


class TestSystemRouter:
    def test_system_stats_with_psutil(self, monkeypatch):
        from markbot.web.routers import system

        # Stub psutil via sys.modules insert in the router's import
        import sys

        class FakeVM:
            total = 1000
            available = 500
            used = 500
            free = 500
            percent = 50.0

        class FakeSwap:
            total = 200
            used = 100
            free = 100
            percent = 50.0

        class FakeDisk:
            total = 300
            used = 150
            free = 150
            percent = 50.0

        class FakeFreq:
            current = 2400.0

        class FakeProc:
            def cpu_percent(self, interval=None):
                return 1.0

            def children(self, recursive=False):
                return []

            def memory_info(self):
                class M:
                    rss = 10
                    vms = 20
                return M()

            def create_time(self):
                return 0.0

            def name(self):
                return "test"

            def cmdline(self):
                return ["pytest"]

            def status(self):
                return "running"

            def oneshot(self):
                return self

            @property
            def pid(self):
                return 1

        class FakePsutil:
            Process = FakeProc
            virtual_memory = staticmethod(lambda: FakeVM())
            swap_memory = staticmethod(lambda: FakeSwap())
            disk_usage = staticmethod(lambda path="/": FakeDisk())
            boot_time = staticmethod(lambda: 0.0)
            cpu_count = staticmethod(lambda logical=True: 4)
            cpu_freq = staticmethod(lambda: FakeFreq())
            cpu_percent = staticmethod(lambda interval=None, percpu=False: 1.0)
            getloadavg = staticmethod(lambda: (0.1, 0.2, 0.3))
            net_io_counters = staticmethod(lambda: type(
                "N", (), {"bytes_sent": 1, "bytes_recv": 2,
                          "packets_sent": 3, "packets_recv": 4})())
            NoSuchProcess = Exception
            AccessDenied = Exception

        monkeypatch.setitem(sys.modules, "psutil", FakePsutil)

        app = FastAPI()
        app.include_router(system.router)
        client = TestClient(app)
        resp = client.get("/api/system/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "memory" in body
        assert body["memory"]["total"] == 1000
        assert body["swap"]["percent"] == 50.0
        assert body["disk"]["total"] == 300
        assert body["cpu"]["count_logical"] == 4
        assert body["load_average"] == [0.1, 0.2, 0.3]
        assert "version" in body

    def test_system_process_returns_process_info(self, monkeypatch):
        import sys

        from markbot.web.routers import system

        class FakeProc:
            @property
            def pid(self):
                return 42

            def cpu_percent(self, interval=None):
                return 0.0

            def children(self, recursive=False):
                return []

            def memory_info(self):
                class M:
                    rss = 5
                    vms = 6
                return M()

            def create_time(self):
                return 0.0

            def name(self):
                return "proc"

            def cmdline(self):
                return []

            def status(self):
                return "running"

            def oneshot(self):
                return self

        class FakePsutil:
            Process = FakeProc
            NoSuchProcess = Exception
            AccessDenied = Exception

        monkeypatch.setitem(sys.modules, "psutil", FakePsutil)

        app = FastAPI()
        app.include_router(system.router)
        app.add_middleware(auth.TokenAuthMiddleware)
        monkeypatch.setattr(auth, "_session_token", "t")
        client = TestClient(app)
        resp = client.get("/api/system/process", headers={"x-markbot-session-token": "t"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["process"]["current"]["pid"] == 42
        assert body["process"]["count"] == 1


# ---------------------------------------------------------------------------
# routers/sessions.py  (uses module-level store; monkeypatch it)
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions_app(tmp_path, monkeypatch):
    from markbot.web.routers import sessions

    fake_store = WebSessionStore(db_path=tmp_path / "sess.db")
    monkeypatch.setattr(sessions, "store", fake_store)
    monkeypatch.setattr(auth, "_session_token", "tok")

    app = FastAPI()
    app.add_middleware(auth.TokenAuthMiddleware)
    app.include_router(sessions.router)
    return app, fake_store


class TestSessionsRouter:
    def test_list_empty(self, sessions_app):
        app, _ = sessions_app
        client = TestClient(app)
        resp = client.get("/api/sessions", headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        assert resp.json() == {"sessions": []}

    def test_require_auth(self, sessions_app):
        app, _ = sessions_app
        client = TestClient(app)
        resp = client.get("/api/sessions")
        assert resp.status_code == 401

    def test_stats_after_messages(self, sessions_app):
        app, store = sessions_app
        store.create_session("a", "t")
        store.add_message("a", "user", "hi")
        client = TestClient(app)
        resp = client.get("/api/sessions/stats", headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["active"] == 1
        assert body["messages"] == 1

    def test_get_session_not_found(self, sessions_app):
        app, _ = sessions_app
        client = TestClient(app)
        resp = client.get("/api/sessions/missing", headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_get_session_found(self, sessions_app):
        app, store = sessions_app
        store.create_session("s1", "Title")
        client = TestClient(app)
        resp = client.get("/api/sessions/s1", headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "s1"
        assert body["title"] == "Title"

    def test_delete_session(self, sessions_app):
        app, store = sessions_app
        store.create_session("s1", "t")
        client = TestClient(app)
        resp = client.delete("/api/sessions/s1", headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_patch_rename(self, sessions_app):
        app, store = sessions_app
        store.create_session("s1", "old")
        client = TestClient(app)
        resp = client.patch(
            "/api/sessions/s1",
            json={"title": "new name"},
            headers={"x-markbot-session-token": "tok"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert store.get_session("s1")["title"] == "new name"

    def test_delete_empty(self, sessions_app):
        app, store = sessions_app
        store.create_session("e", "t")
        store.create_session("f", "t")
        store.add_message("f", "user", "x")
        client = TestClient(app)
        resp = client.delete("/api/sessions/empty", headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

    def test_bulk_delete(self, sessions_app):
        app, store = sessions_app
        store.create_session("s0", "t")
        store.create_session("s1", "t")
        client = TestClient(app)
        resp = client.post(
            "/api/sessions/bulk-delete",
            json={"ids": ["s0", "s1"]},
            headers={"x-markbot-session-token": "tok"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2

    def test_search(self, sessions_app):
        app, store = sessions_app
        store.create_session("s1", "alpha")
        store.add_message("s1", "user", "findme word")
        client = TestClient(app)
        resp = client.get("/api/sessions/search", params={"q": "findme"},
                         headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["sessions"]}
        assert "s1" in ids

    def test_export_markdown(self, sessions_app):
        app, store = sessions_app
        store.create_session("s1", "Export Test")
        store.add_message("s1", "user", "hello")
        client = TestClient(app)
        resp = client.get("/api/sessions/s1/export",
                         headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]
        assert "Export Test" in resp.text
        assert "attachment" in resp.headers["content-disposition"]

    def test_export_json(self, sessions_app):
        app, store = sessions_app
        store.create_session("s1", "T")
        client = TestClient(app)
        resp = client.get("/api/sessions/s1/export", params={"format": "json"},
                         headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "s1"

    def test_export_missing(self, sessions_app):
        app, _ = sessions_app
        client = TestClient(app)
        resp = client.get("/api/sessions/missing/export",
                         headers={"x-markbot-session-token": "tok"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# routers/config.py  (mock load_config / save_config)
# ---------------------------------------------------------------------------


class TestConfigRouter:
    def test_get_config(self, monkeypatch):
        from markbot.web.routers import config

        class FakeCfg:
            def model_dump(self, mode="json", by_alias=False):
                return {"workspace_path": "/tmp", "agents": {"defaults": {}}}

        monkeypatch.setattr(config, "load_config", lambda: FakeCfg())
        app = FastAPI()
        app.add_middleware(auth.TokenAuthMiddleware)
        app.include_router(config.router)
        monkeypatch.setattr(auth, "_session_token", "t")
        client = TestClient(app)
        resp = client.get("/api/config", headers={"x-markbot-session-token": "t"})
        assert resp.status_code == 200
        assert resp.json()["workspace_path"] == "/tmp"

    def test_get_raw_config_no_file(self, monkeypatch):
        from markbot.web.routers import config

        monkeypatch.setattr(config, "load_config", lambda: None)
        import markbot.config.loader as loader
        monkeypatch.setattr(loader, "get_config_path", lambda: None)

        app = FastAPI()
        app.add_middleware(auth.TokenAuthMiddleware)
        app.include_router(config.router)
        monkeypatch.setattr(auth, "_session_token", "t")
        client = TestClient(app)
        resp = client.get("/api/config/raw", headers={"x-markbot-session-token": "t"})
        assert resp.status_code == 200
        assert resp.json() == {"raw": ""}


# ---------------------------------------------------------------------------
# routers/env.py — pure functions
# ---------------------------------------------------------------------------


class TestEnvHelpers:
    def test_is_secret_detects_patterns(self):
        from markbot.web.routers.env import _is_secret
        assert _is_secret("API_KEY") is True
        assert _is_secret("MY_PASSWORD") is True
        assert _is_secret("auth_token") is True
        assert _is_secret("DB_HOST") is False

    def test_parse_env_file(self, tmp_path):
        from markbot.web.routers.env import _parse_env_file
        p = tmp_path / ".env"
        p.write_text('# comment\nFOO=bar\nBAZ="hello world"\nEMPTY=\n', encoding="utf-8")
        result = _parse_env_file(p)
        assert result == {"FOO": "bar", "BAZ": "hello world", "EMPTY": ""}

    def test_parse_env_file_missing_returns_empty(self, tmp_path):
        from markbot.web.routers.env import _parse_env_file
        assert _parse_env_file(tmp_path / "nope.env") == {}

    def test_write_then_read_roundtrip(self, tmp_path):
        from markbot.web.routers.env import _parse_env_file, _write_env_file
        p = tmp_path / ".env"
        _write_env_file(p, {"SIMPLE": "val", "SPACED": "a b", "QUOTED": 'start"end'})
        parsed = _parse_env_file(p)
        assert parsed["SIMPLE"] == "val"
        assert parsed["SPACED"] == "a b"
        assert parsed["QUOTED"] == 'start"end'


# ---------------------------------------------------------------------------
# routers/logs.py
# ---------------------------------------------------------------------------


class TestLogsRouter:
    def test_list_log_files(self, tmp_path, monkeypatch):
        from markbot.web.routers import logs

        (tmp_path / "a.log").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        (tmp_path / "ignore.bin").write_text("z")
        monkeypatch.setattr(logs, "_resolve_log_dir", lambda: tmp_path)

        app = FastAPI()
        app.add_middleware(auth.TokenAuthMiddleware)
        app.include_router(logs.router)
        monkeypatch.setattr(auth, "_session_token", "t")
        client = TestClient(app)
        resp = client.get("/api/logs/files", headers={"x-markbot-session-token": "t"})
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert "a.log" in files
        assert "b.txt" in files
        assert "ignore.bin" not in files

    def test_get_logs_content_with_filters(self, tmp_path, monkeypatch):
        from markbot.web.routers import logs

        (tmp_path / "markbot.log").write_text(
            "INFO start\nERROR boom\nDEBUG x\nINFO done\n", encoding="utf-8"
        )
        monkeypatch.setattr(logs, "_resolve_log_dir", lambda: tmp_path)

        app = FastAPI()
        app.add_middleware(auth.TokenAuthMiddleware)
        app.include_router(logs.router)
        monkeypatch.setattr(auth, "_session_token", "t")
        client = TestClient(app)
        resp = client.get("/api/logs", params={"lines": 10, "level": "ERROR"},
                          headers={"x-markbot-session-token": "t"})
        assert resp.status_code == 200
        body = resp.json()
        assert any("ERROR" in e for e in body["logs"])
        assert all("DEBUG" not in e for e in body["logs"])

    def test_get_logs_missing_dir(self, tmp_path, monkeypatch):
        from markbot.web.routers import logs

        missing = tmp_path / "nope"
        monkeypatch.setattr(logs, "_resolve_log_dir", lambda: missing)

        app = FastAPI()
        app.add_middleware(auth.TokenAuthMiddleware)
        app.include_router(logs.router)
        monkeypatch.setattr(auth, "_session_token", "t")
        client = TestClient(app)
        resp = client.get("/api/logs", headers={"x-markbot-session-token": "t"})
        assert resp.status_code == 200
        assert resp.json()["logs"] == []
