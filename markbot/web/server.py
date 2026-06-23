"""Markbot Web UI server — app factory."""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from starlette.routing import WebSocketRoute

_log = logging.getLogger(__name__)

WEB_DIST = Path(__file__).parent / "static"
_chat_sessions: dict[str, dict[str, Any]] = {}


def _build_app():
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, HTMLResponse, Response as FastResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.websockets import WebSocket

    from markbot.web.auth import TokenAuthMiddleware, get_token, regenerate_token, verify_ws_token
    from markbot.web.store import WebSessionStore
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

    app = FastAPI(title="Markbot", version="1.0")
    app.add_middleware(TokenAuthMiddleware)

    store = WebSessionStore()

    _agent_loop = None
    _agent_lock = None
    _upload_dir: Path | None = None

    async def _get_agent():
        nonlocal _agent_loop, _agent_lock
        if _agent_loop is not None:
            return _agent_loop
        if _agent_lock is None:
            from asyncio import Lock
            _agent_lock = Lock()
        async with _agent_lock:
            if _agent_loop is not None:
                return _agent_loop
            _agent_loop = await _create_agent_loop()
            return _agent_loop

    async def _create_agent_loop():
        from markbot.agent.loop import AgentLoop
        from markbot.bus.queue import MessageBus
        from markbot.config.loader import load_config
        from markbot.config.paths import get_cron_dir
        from markbot.schedule.cron import CronJob, CronService
        from markbot.session.session import SessionManager

        config = load_config()
        bus = MessageBus()

        from markbot.cli.runtime import make_provider
        provider = make_provider(config)

        cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
        cron = CronService(cron_store_path)

        session_manager = SessionManager(config.workspace_path)

        loop = AgentLoop(
            ctx_or_bus=bus,
            fallback_manager=provider,
            config=config,
            workspace=config.workspace_path,
            max_iterations=config.agents.defaults.max_tool_iterations,
            context_window_tokens=config.agents.defaults.context_window_tokens,
            web_search_config=config.tools.web.search,
            web_proxy=config.tools.web.proxy or None,
            exec_config=config.tools.exec,
            filesystem_config=config.tools.filesystem,
            memory_config=config.tools.memory,
            cron_service=cron,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=session_manager,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=config.agents.defaults.timezone,
            compaction_config=config.compaction,
            max_budget_usd=config.budget.max_budget_usd if config.budget.enabled else None,
            warn_threshold_usd=config.budget.warn_threshold_usd,
            budget_config=config.budget if config.budget.enabled else None,
        )

        # Wire up cron job execution callback (mirrors gateway behavior)
        async def _on_cron_job(job: CronJob) -> str | None:
            from markbot.tools.cron import CronTool

            reminder_note = (
                "[Scheduled Task] Timer finished.\n\n"
                f"Task '{job.name}' has been triggered.\n"
                f"Scheduled instruction: {job.payload.message}"
            )

            # Prevent cron jobs from recursively scheduling new jobs
            cron_tool = loop.tools.get("cron")
            cron_token = None
            if isinstance(cron_tool, CronTool):
                cron_token = cron_tool.set_cron_context(True)
            try:
                resp = await loop.process_direct(
                    reminder_note,
                    session_key=f"cron:{job.id}",
                    channel=job.payload.channel or "web",
                    chat_id=job.payload.to or "direct",
                )
            finally:
                if isinstance(cron_tool, CronTool) and cron_token is not None:
                    cron_tool.reset_cron_context(cron_token)
            return resp.content if resp else ""

        cron.on_job = _on_cron_job

        # Start the cron timer so scheduled jobs actually fire in web mode
        await cron.start()

        # Share the cron service with the router so API operations use the
        # same in-memory store as the timer (avoids data races).
        from markbot.web.routers.cron import set_cron_service
        set_cron_service(cron)

        return loop

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
                    store.add_message(session_id, "user", user_display)
                    msgs.append({
                        "role": "user", "content": user_display,
                        "timestamp": time.time(), "media": edit_media,
                    })
                    sess_dict["last_active"] = time.time()
                    if len(msgs) == 1:
                        store.update_title(session_id, user_display[:60])
                        sess_dict["title"] = user_display[:60]
                    await websocket.send_text(json.dumps({"type": "messages_trimmed", "timestamp": edit_timestamp}))
                    await websocket.send_text(json.dumps({"type": "session", "session_id": session_id}))
                    collected_chunks: list[str] = []
                    collected_media: list[str] = []
                    stop_flag["stop"] = False
                    async def on_stream_edit(delta: str):
                        if stop_flag["stop"]:
                            raise RuntimeError("Streaming stopped by user")
                        collected_chunks.append(delta)
                        try:
                            await websocket.send_text(json.dumps({"type": "stream_delta", "delta": delta}))
                        except Exception as exc:
                            _log.debug("WebSocket send stream_delta failed: %s", exc)
                    async def on_progress_edit(text: str, **kw: object):
                        try:
                            await websocket.send_text(json.dumps({"type": "progress", "content": text}))
                        except Exception as exc:
                            _log.debug("WebSocket send progress failed: %s", exc)
                    async def on_tool_start_edit(tool_id: str, name: str, context: str | None):
                        try:
                            await websocket.send_text(json.dumps({"type": "tool_start", "tool_id": tool_id, "name": name, "context": context}))
                        except Exception as exc:
                            _log.debug("WebSocket send tool_start failed: %s", exc)
                    async def on_tool_complete_edit(tool_id: str, name: str, summary: str | None, error: str | None):
                        try:
                            await websocket.send_text(json.dumps({"type": "tool_complete", "tool_id": tool_id, "name": name, "summary": summary, "error": error}))
                        except Exception as exc:
                            _log.debug("WebSocket send tool_complete failed: %s", exc)
                    async def on_outbound_edit(om):
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
                            edit_content, session_key=f"web:{session_id}",
                            channel="web", chat_id=session_id, media=edit_media,
                            on_progress=on_progress_edit, on_stream=on_stream_edit,
                            on_tool_start=on_tool_start_edit, on_tool_complete=on_tool_complete_edit,
                            on_outbound_message=on_outbound_edit,
                        )
                        if collected_chunks:
                            full_content = "".join(collected_chunks)
                            store.add_message(session_id, "assistant", full_content)
                            msgs.append({"role": "assistant", "content": full_content, "timestamp": time.time(), "media": collected_media})
                            payload: dict[str, Any] = {"type": "stream_end"}
                            if collected_media:
                                payload["media"] = collected_media
                            await websocket.send_text(json.dumps(payload))
                        else:
                            content = response.content if response else ""
                            store.add_message(session_id, "assistant", content)
                            msgs.append({"role": "assistant", "content": content, "timestamp": time.time(), "media": collected_media})
                            payload = {"type": "message", "content": content, "metadata": response.metadata if response else {}}
                            if collected_media:
                                payload["media"] = collected_media
                            await websocket.send_text(json.dumps(payload))
                        sess_dict["last_active"] = time.time()
                    except Exception as e:
                        _log.exception("Error processing edited message")
                        await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
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
                    collected_chunks = []
                    collected_media = []
                    stop_flag["stop"] = False
                    async def on_stream_reg(delta: str):
                        if stop_flag["stop"]:
                            raise RuntimeError("Streaming stopped by user")
                        collected_chunks.append(delta)
                        try:
                            await websocket.send_text(json.dumps({"type": "stream_delta", "delta": delta}))
                        except Exception as exc:
                            _log.debug("WebSocket send stream_delta failed: %s", exc)
                    async def on_progress_reg(text: str, **kw: object):
                        try:
                            await websocket.send_text(json.dumps({"type": "progress", "content": text}))
                        except Exception as exc:
                            _log.debug("WebSocket send progress failed: %s", exc)
                    async def on_tool_start_reg(tool_id: str, name: str, context: str | None):
                        try:
                            await websocket.send_text(json.dumps({"type": "tool_start", "tool_id": tool_id, "name": name, "context": context}))
                        except Exception as exc:
                            _log.debug("WebSocket send tool_start failed: %s", exc)
                    async def on_tool_complete_reg(tool_id: str, name: str, summary: str | None, error: str | None):
                        try:
                            await websocket.send_text(json.dumps({"type": "tool_complete", "tool_id": tool_id, "name": name, "summary": summary, "error": error}))
                        except Exception as exc:
                            _log.debug("WebSocket send tool_complete failed: %s", exc)
                    async def on_outbound_reg(om):
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
                            user_content, session_key=f"web:{session_id}",
                            channel="web", chat_id=session_id, media=user_media_list,
                            on_progress=on_progress_reg, on_stream=on_stream_reg,
                            on_tool_start=on_tool_start_reg, on_tool_complete=on_tool_complete_reg,
                            on_outbound_message=on_outbound_reg,
                        )
                        if collected_chunks:
                            full_content = "".join(collected_chunks)
                            store.add_message(session_id, "assistant", full_content)
                            msgs.append({"role": "assistant", "content": full_content, "timestamp": time.time(), "media": collected_media})
                            payload = {"type": "stream_end"}
                            if collected_media:
                                payload["media"] = collected_media
                            await websocket.send_text(json.dumps(payload))
                        else:
                            content = response.content if response else ""
                            store.add_message(session_id, "assistant", content)
                            msgs.append({"role": "assistant", "content": content, "timestamp": time.time(), "media": collected_media})
                            payload = {"type": "message", "content": content, "metadata": response.metadata if response else {}}
                            if collected_media:
                                payload["media"] = collected_media
                            await websocket.send_text(json.dumps(payload))
                        sess_dict["last_active"] = time.time()
                    except Exception as e:
                        _log.exception("Error regenerating message")
                        await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
                    continue

                user_content = data.get("content", "").strip()
                user_media: list[str] = data.get("media", [])

                if not user_content and not user_media:
                    continue

                user_display = user_content or "(附件)"
                _ensure_session()

                store.add_message(session_id, "user", user_display)
                sess_dict = _chat_sessions.setdefault(session_id, {})
                sess_dict.setdefault("messages", []).append({
                    "role": "user", "content": user_display, "timestamp": time.time(),
                    "media": user_media,
                })
                sess_dict["last_active"] = time.time()

                if len(sess_dict["messages"]) == 1:
                    store.update_title(session_id, user_display[:60])
                    sess_dict["title"] = user_display[:60]

                await websocket.send_text(json.dumps({"type": "session", "session_id": session_id}))

                collected_chunks: list[str] = []
                collected_media: list[str] = []
                # Reset stop flag at the start of a new turn
                stop_flag["stop"] = False

                async def on_stream(delta: str):
                    if stop_flag["stop"]:
                        raise RuntimeError("Streaming stopped by user")
                    collected_chunks.append(delta)
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "stream_delta", "delta": delta,
                        }))
                    except Exception as exc:
                        _log.debug("WebSocket send stream_delta failed: %s", exc)

                async def on_progress(text: str, **kwargs: object):
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "progress", "content": text,
                        }))
                    except Exception as exc:
                        _log.debug("WebSocket send progress failed: %s", exc)

                async def on_tool_start(tool_id: str, name: str, context: str | None):
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_start", "tool_id": tool_id, "name": name, "context": context,
                        }))
                    except Exception as exc:
                        _log.debug("WebSocket send tool_start failed: %s", exc)

                async def on_tool_complete(tool_id: str, name: str, summary: str | None, error: str | None):
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_complete", "tool_id": tool_id, "name": name,
                            "summary": summary, "error": error,
                        }))
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
                        sess_dict["messages"].append({
                            "role": "assistant", "content": full_content, "timestamp": time.time(),
                            "media": collected_media,
                        })
                        payload: dict[str, Any] = {"type": "stream_end"}
                        if collected_media:
                            payload["media"] = collected_media
                        await websocket.send_text(json.dumps(payload))
                    else:
                        content = response.content if response else ""
                        store.add_message(session_id, "assistant", content)
                        sess_dict["messages"].append({
                            "role": "assistant", "content": content, "timestamp": time.time(),
                            "media": collected_media,
                        })
                        payload = {
                            "type": "message", "content": content,
                            "metadata": response.metadata if response else {},
                        }
                        if collected_media:
                            payload["media"] = collected_media
                        await websocket.send_text(json.dumps(payload))

                    sess_dict["last_active"] = time.time()

                except Exception as e:
                    _log.exception("Error processing message")
                    await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))

        except Exception:
            _log.info("WebSocket disconnected")

    app.routes.insert(0, WebSocketRoute("/api/ws/chat", endpoint=ws_chat_handler))

    async def _ensure_upload_dir():
        nonlocal _upload_dir
        if _upload_dir is not None:
            return _upload_dir
        from markbot.config.loader import load_config
        cfg = load_config()
        d = Path(cfg.workspace_path) / ".web_uploads"
        d.mkdir(parents=True, exist_ok=True)
        _upload_dir = d
        return d

    from fastapi import File, UploadFile as FastAPIUploadFile

    @app.post("/api/upload")
    async def upload_file(file: FastAPIUploadFile = File(...)):
        upload_dir = await _ensure_upload_dir()
        name = f"{secrets.token_urlsafe(8)}_{file.filename or 'file'}"
        dest = upload_dir / name
        content = await file.read()
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


def start_server(host: str = "127.0.0.1", port: int = 9120):
    import uvicorn
    app = _build_app()
    print("\n  Markbot Web UI")
    print(f"  Listening on http://{host}:{port}")
    print("  Press Ctrl+C to stop\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
