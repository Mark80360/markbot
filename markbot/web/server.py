"""Markbot Web UI server — app factory."""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from starlette.routing import WebSocketRoute

from fastapi import File, UploadFile

from markbot.web.auth import get_token

_log = logging.getLogger(__name__)

WEB_DIST = Path(__file__).parent / "static"
_chat_sessions: dict[str, dict[str, Any]] = {}

# When True, the cached agent loop is stale (e.g. env vars changed) and
# _get_agent will rebuild it on the next call. Set via invalidate_agent().
_agent_invalidated = False


def invalidate_agent() -> None:
    """Mark the cached agent loop as stale so it is rebuilt on next use.

    Called after env-var changes (API keys, etc.) so the new values are
    picked up by the provider on the next request.
    """
    global _agent_invalidated
    _agent_invalidated = True


def _build_app(workspace: str | Path | None = None):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response as FastResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.websockets import WebSocket

    from markbot.web.auth import TokenAuthMiddleware, get_token, verify_ws_token
    from markbot.web.store import get_store
    from markbot.web.routers.status import router as status_router
    from markbot.web.routers.config import router as config_router
    from markbot.web.routers.env import router as env_router
    from markbot.web.routers.sessions import router as sessions_router
    from markbot.web.routers.models import router as models_router
    from markbot.web.routers.logs import router as logs_router
    from markbot.web.routers.skills import router as skills_router
    from markbot.web.routers.cron import router as cron_router
    from markbot.web.routers.channels import router as channels_router
    from markbot.web.routers.system import router as system_router
    from markbot.web.routers.mcp import router as mcp_router

    workspace_override = Path(workspace).expanduser().resolve() if workspace else None

    def _load_config():
        from markbot.config.loader import load_config

        cfg = load_config()
        if workspace_override is not None:
            cfg.agents.defaults.workspace = str(workspace_override)
        return cfg

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        # Agent runtime is lazy-initialized on first request, so startup is a
        # no-op. On shutdown, stop any runtime we started so cron / heartbeat /
        # MCP resources are released cleanly instead of leaking timers.
        yield
        runtime = getattr(app.state, "agent_runtime", None)
        if runtime is not None:
            try:
                await runtime.stop()
                _log.info("Agent runtime stopped on shutdown")
            except Exception:
                _log.warning("Failed to stop agent runtime on shutdown", exc_info=True)

    app = FastAPI(title="Markbot", version="1.0", lifespan=lifespan)
    app.add_middleware(TokenAuthMiddleware)

    store = get_store()

    _agent_loop = None
    _agent_lock = None
    _upload_dir: Path | None = None

    async def _get_agent():
        nonlocal _agent_loop, _agent_lock
        global _agent_invalidated
        if _agent_loop is not None and not _agent_invalidated:
            return _agent_loop
        if _agent_lock is None:
            from asyncio import Lock
            _agent_lock = Lock()
        async with _agent_lock:
            if _agent_loop is not None and not _agent_invalidated:
                return _agent_loop
            # Stop the previous runtime (cron/heartbeat/MCP) before rebuilding
            # so we don't leak timers or run cron jobs twice.
            prev_runtime = getattr(app.state, "agent_runtime", None)
            if prev_runtime is not None:
                try:
                    await prev_runtime.stop()
                except Exception:
                    _log.warning("Failed to stop previous agent runtime", exc_info=True)
                app.state.agent_runtime = None
            _agent_loop = await _create_agent_loop()
            _agent_invalidated = False
            return _agent_loop

    async def _create_agent_loop():
        from markbot.cli.runtime import make_provider
        from markbot.runtime import WEB_FEATURES, build_runtime

        config = _load_config()
        # Web profile: cron runner (log-only failures, no channel deliver),
        # no heartbeat/dream/channels. AgentLoop params match gateway.
        runtime = build_runtime(config, WEB_FEATURES, make_provider=make_provider)
        await runtime.start_cron()

        # Share the cron service with the router so API operations use the
        # same in-memory store as the timer (avoids data races).
        if runtime.cron is not None:
            from markbot.web.routers.cron import set_cron_service

            set_cron_service(runtime.cron)

        # Keep runtime reachable for clean shutdown if we add lifespan later.
        app.state.agent_runtime = runtime
        return runtime.agent

    # Register core routers
    app.include_router(status_router)
    app.include_router(config_router)
    app.include_router(env_router)
    app.include_router(sessions_router)
    app.include_router(models_router)
    app.include_router(logs_router)
    app.include_router(skills_router)
    app.include_router(cron_router)
    app.include_router(channels_router)
    app.include_router(system_router)
    app.include_router(mcp_router)

    async def ws_chat_handler(websocket: WebSocket):
        if not await verify_ws_token(websocket):
            _log.warning("WebSocket rejected: invalid token")
            await websocket.close(code=4401)
            return

        await websocket.accept()
        _log.info("WebSocket accepted")
        agent = await _get_agent()

        session_id: str | None = None
        # Shared stop flag across the connection; set by stop_streaming,
        # checked and cleared by the streaming loop.
        stop_flag: dict[str, bool] = {"stop": False}

        def _ensure_session():
            nonlocal session_id
            if session_id is None:
                session_id = secrets.token_urlsafe(8)
                s = store.create_session(session_id, "新对话")
                _chat_sessions[session_id] = {
                    "title": "新对话",
                    "messages": [],
                    "created_at": s["created_at"],
                    "last_active": s["last_active"],
                }

        async def _finalize_stopped(chunks: list[str], media_list: list[str]):
            """Persist partial streamed content when the user stops mid-stream.

            Saves what was collected so far so the assistant turn survives a
            page refresh, then signals stream_end with stopped=True.
            """
            if chunks:
                full = "".join(chunks)
                store.add_message(session_id, "assistant", full)
                sess = _chat_sessions.setdefault(session_id, {})
                sess.setdefault("messages", []).append({
                    "role": "assistant", "content": full,
                    "timestamp": time.time(), "media": media_list,
                })
                sess["last_active"] = time.time()
                payload: dict[str, Any] = {"type": "stream_end", "stopped": True}
                if media_list:
                    payload["media"] = media_list
                await websocket.send_text(json.dumps(payload))
            else:
                await websocket.send_text(json.dumps({"type": "stream_end", "stopped": True}))

        async def _run_assistant_turn(user_content: str, user_media: list[str]) -> None:
            """Run one assistant turn: stream deltas, persist, handle stop/error.

            Shared by the normal, edit_and_resend, and regenerate paths so the
            streaming callbacks, response persistence, and stop/error handling
            exist in exactly one place. Assumes ``session_id`` is set and the
            user message has already been recorded by the caller.
            """
            # Re-fetch the agent each turn: env-var changes set
            # _agent_invalidated, and _get_agent rebuilds the runtime when
            # that flag is set, so the new API key / model takes effect on
            # the next message without forcing the client to reconnect.
            agent = await _get_agent()
            collected_chunks: list[str] = []
            collected_media: list[str] = []
            stop_flag["stop"] = False

            async def on_stream(delta: str):
                if stop_flag["stop"]:
                    raise RuntimeError("Streaming stopped by user")
                collected_chunks.append(delta)
                try:
                    await websocket.send_text(json.dumps({"type": "stream_delta", "delta": delta}))
                except Exception as exc:
                    _log.debug("WebSocket send stream_delta failed: %s", exc)

            async def on_progress(text: str, **kwargs: object):
                try:
                    await websocket.send_text(json.dumps({"type": "progress", "content": text}))
                except Exception as exc:
                    _log.debug("WebSocket send progress failed: %s", exc)

            async def on_tool_start(tool_id: str, name: str, context: str | None):
                try:
                    await websocket.send_text(json.dumps({"type": "tool_start", "tool_id": tool_id, "name": name, "context": context}))
                except Exception as exc:
                    _log.debug("WebSocket send tool_start failed: %s", exc)

            async def on_tool_complete(tool_id: str, name: str, summary: str | None, error: str | None):
                try:
                    await websocket.send_text(json.dumps({"type": "tool_complete", "tool_id": tool_id, "name": name, "summary": summary, "error": error}))
                except Exception as exc:
                    _log.debug("WebSocket send tool_complete failed: %s", exc)

            async def on_outbound_message(om):
                for fp in (om.media or []):
                    p = Path(fp)
                    if p.exists():
                        upload_dir = await _ensure_upload_dir()
                        name = f"gen_{secrets.token_urlsafe(8)}_{p.name}"
                        shutil.copy2(fp, upload_dir / name)
                        collected_media.append(f"/api/media/{name}")
                if om.content and not collected_chunks:
                    collected_chunks.append(om.content)

            try:
                response = await agent.process_direct(
                    user_content,
                    session_key=f"web:{session_id}",
                    channel="web",
                    chat_id=session_id,
                    media=user_media,
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_tool_start=on_tool_start,
                    on_tool_complete=on_tool_complete,
                    on_outbound_message=on_outbound_message,
                )

                if collected_chunks:
                    full_content = "".join(collected_chunks)
                    store.add_message(session_id, "assistant", full_content)
                    asst_ts = time.time()
                    sess = _chat_sessions.setdefault(session_id, {})
                    sess.setdefault("messages", []).append({
                        "role": "assistant", "content": full_content,
                        "timestamp": asst_ts, "media": collected_media,
                    })
                    payload: dict[str, Any] = {"type": "stream_end", "timestamp": asst_ts}
                    if collected_media:
                        payload["media"] = collected_media
                    await websocket.send_text(json.dumps(payload))
                else:
                    content = response.content if response else ""
                    store.add_message(session_id, "assistant", content)
                    asst_ts = time.time()
                    sess = _chat_sessions.setdefault(session_id, {})
                    sess.setdefault("messages", []).append({
                        "role": "assistant", "content": content,
                        "timestamp": asst_ts, "media": collected_media,
                    })
                    payload = {
                        "type": "message", "content": content,
                        "metadata": response.metadata if response else {},
                        "timestamp": asst_ts,
                    }
                    if collected_media:
                        payload["media"] = collected_media
                    await websocket.send_text(json.dumps(payload))

                sess["last_active"] = time.time()

            except RuntimeError as e:
                if "Streaming stopped by user" in str(e):
                    await _finalize_stopped(collected_chunks, collected_media)
                else:
                    _log.exception("Error processing assistant turn")
                    await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
            except Exception as e:
                _log.exception("Error processing assistant turn")
                await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"content": raw}

                msg_type = data.get("type", "")

                if msg_type == "stop_streaming":
                    stop_flag["stop"] = True
                    continue

                if msg_type == "new_session":
                    session_id = None
                    await websocket.send_text(json.dumps({"type": "session_cleared"}))
                    continue

                if msg_type == "resume_session":
                    target = data.get("session_id", "")
                    if target:
                        sess = store.get_session(target)
                        if sess:
                            session_id = target
                            _chat_sessions[target] = sess
                            await websocket.send_text(json.dumps({
                                "type": "session",
                                "session_id": session_id,
                            }))
                    continue

                if msg_type == "edit_and_resend":
                    edit_content = data.get("content", "").strip()
                    edit_media: list[str] = data.get("media", [])
                    edit_timestamp = data.get("timestamp", 0)
                    if not edit_content and not edit_media:
                        continue
                    if not session_id:
                        _ensure_session()
                    store.delete_messages_from(session_id, edit_timestamp)
                    sess_dict = _chat_sessions.get(session_id, {})
                    msgs = sess_dict.get("messages", [])
                    while msgs and msgs[-1].get("timestamp", 0) >= edit_timestamp:
                        msgs.pop()
                    user_display = edit_content or "(附件)"
                    user_ts = time.time()
                    store.add_message(session_id, "user", user_display)
                    msgs.append({
                        "role": "user", "content": user_display,
                        "timestamp": user_ts, "media": edit_media,
                    })
                    sess_dict["last_active"] = time.time()
                    if len(msgs) == 1:
                        store.update_title(session_id, user_display[:60])
                        sess_dict["title"] = user_display[:60]
                    await websocket.send_text(json.dumps({"type": "messages_trimmed", "timestamp": edit_timestamp}))
                    await websocket.send_text(json.dumps({"type": "session", "session_id": session_id, "user_ts": user_ts}))
                    await _run_assistant_turn(edit_content, edit_media)
                    continue

                if msg_type == "regenerate":
                    reg_timestamp = data.get("timestamp", 0)
                    if not session_id:
                        continue
                    store.delete_messages_from(session_id, reg_timestamp)
                    sess_dict = _chat_sessions.get(session_id, {})
                    msgs = sess_dict.get("messages", [])
                    while msgs and msgs[-1].get("timestamp", 0) >= reg_timestamp:
                        msgs.pop()
                    await websocket.send_text(json.dumps({"type": "messages_trimmed", "timestamp": reg_timestamp}))
                    await websocket.send_text(json.dumps({"type": "session", "session_id": session_id}))
                    last_user_msg = next(
                        (m for m in reversed(msgs) if m["role"] == "user"), None
                    )
                    if not last_user_msg:
                        await websocket.send_text(json.dumps({"type": "error", "content": "No user message to regenerate from"}))
                        continue
                    user_content = last_user_msg["content"]
                    user_media_list: list[str] = last_user_msg.get("media", [])
                    await _run_assistant_turn(user_content, user_media_list)
                    continue

                user_content = data.get("content", "").strip()
                user_media: list[str] = data.get("media", [])

                if not user_content and not user_media:
                    continue

                user_display = user_content or "(附件)"
                _ensure_session()

                user_ts = time.time()
                store.add_message(session_id, "user", user_display)
                sess_dict = _chat_sessions.setdefault(session_id, {})
                sess_dict.setdefault("messages", []).append({
                    "role": "user", "content": user_display, "timestamp": user_ts,
                    "media": user_media,
                })
                sess_dict["last_active"] = time.time()

                if len(sess_dict["messages"]) == 1:
                    store.update_title(session_id, user_display[:60])
                    sess_dict["title"] = user_display[:60]

                await websocket.send_text(json.dumps({"type": "session", "session_id": session_id, "user_ts": user_ts}))

                await _run_assistant_turn(user_content, user_media)

        except Exception:
            _log.debug("WebSocket disconnected", exc_info=True)

    app.routes.insert(0, WebSocketRoute("/api/ws/chat", endpoint=ws_chat_handler))

    async def _ensure_upload_dir():
        nonlocal _upload_dir
        if _upload_dir is not None:
            return _upload_dir
        cfg = _load_config()
        d = Path(cfg.workspace_path) / ".web_uploads"
        d.mkdir(parents=True, exist_ok=True)
        _upload_dir = d
        return d

    @app.post("/api/upload")
    async def upload_file(file: UploadFile = File(...)):
        upload_dir = await _ensure_upload_dir()
        # Sanitize filename: take basename and normalize any path separators
        # (POSIX "/" and Windows "\") so client-controlled filenames cannot
        # escape upload_dir via traversal or absolute-path override.
        raw_name = file.filename or "file"
        safe_base = re.sub(r"[\\/]+", "_", Path(raw_name).name)
        if safe_base in ("", ".", ".."):
            safe_base = "file"
        name = f"{secrets.token_urlsafe(8)}_{safe_base}"
        dest = upload_dir / name
        content = await file.read()
        # Cap upload size to protect the process from OOM via oversized uploads.
        MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
        if len(content) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                {"error": f"File too large (max {MAX_UPLOAD_SIZE // (1024 * 1024)} MB)"},
                status_code=413,
            )
        dest.write_bytes(content)
        return {"url": f"/api/media/{name}", "name": name}

    @app.get("/api/media/{path:path}")
    async def serve_media(path: str):
        upload_dir = await _ensure_upload_dir()
        file_path = (upload_dir / path).resolve()
        # Prevent path traversal: ensure resolved path is within upload_dir
        try:
            file_path.relative_to(upload_dir.resolve())
        except ValueError:
            return FastResponse(status_code=403)
        if not file_path.exists() or not file_path.is_file():
            return FastResponse(status_code=404)
        ext = file_path.suffix.lower()
        media_types = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
            ".pdf": "application/pdf", ".mp3": "audio/mpeg", ".mp4": "video/mp4",
            ".txt": "text/plain", ".json": "application/json",
        }
        mt = media_types.get(ext, "application/octet-stream")
        return FileResponse(str(file_path), media_type=mt)

    def _spa_response():
        content = (WEB_DIST / "index.html").read_bytes()
        html = content.decode("utf-8")
        html = html.replace(
            "</head>",
            f'<script>window.__MARKBOT_SESSION_TOKEN__="{get_token()}";</script></head>',
            1,
        )
        return FastResponse(
            html.encode("utf-8"),
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="static-assets")

        @app.get("/")
        async def serve_index():
            return _spa_response()

        # Register favicon BEFORE the catch-all SPA route so it isn't shadowed
        @app.get("/favicon.ico")
        async def serve_favicon():
            favicon = WEB_DIST / "favicon.ico"
            if favicon.exists():
                return FileResponse(favicon)
            return FastResponse(status_code=204)

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            if full_path.startswith("api/"):
                return FastResponse(status_code=404)
            return _spa_response()
    else:
        @app.get("/")
        async def no_frontend():
            return HTMLResponse(
                "<h1>Markbot Web UI</h1>"
                "<p>Frontend not built. Run: <code>cd web && npm run build</code></p>"
            )

    return app


def start_server(host: str = "127.0.0.1", port: int = 9120, workspace: str | Path | None = None):
    import uvicorn
    app = _build_app(workspace=workspace)
    print("\n  Markbot Web UI")
    print(f"  Listening on http://{host}:{port}")
    print(f"  Token: {get_token()}")
    print("  Press Ctrl+C to stop\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
