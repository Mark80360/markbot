"""Markbot Web UI server.

Provides a FastAPI backend serving the React frontend and WebSocket
endpoint for real-time chat with the Markbot agent.

Usage:
    markbot web                  # Start on http://127.0.0.1:9120
    markbot web --port 8080
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

WEB_DIST = Path(__file__).parent / "static"

_session_token = secrets.token_urlsafe(32)

_chat_sessions: dict[str, dict[str, Any]] = {}


def _build_app():
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    app = FastAPI(title="Markbot", version="1.0")

    _agent_loop = None
    _agent_lock = asyncio.Lock()

    async def _get_agent():
        nonlocal _agent_loop
        if _agent_loop is not None:
            return _agent_loop
        async with _agent_lock:
            if _agent_loop is not None:
                return _agent_loop
            _agent_loop = await _create_agent_loop()
            return _agent_loop

    async def _create_agent_loop():
        from markbot.agent.loop import AgentLoop
        from markbot.bus.queue import MessageBus
        from markbot.config.loader import load_config
        from markbot.schedule.cron import CronService

        config = load_config()
        bus = MessageBus()

        from markbot.cli.commands import _make_provider
        provider = _make_provider(config)

        cron_store_path = Path(config.workspace_path) / ".cron" / "jobs.json"
        cron = CronService(cron_store_path)

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
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=config.agents.defaults.timezone,
            compaction_config=config.compaction,
            max_budget_usd=config.budget.max_budget_usd if config.budget.enabled else None,
            warn_threshold_usd=config.budget.warn_threshold_usd,
            budget_config=config.budget if config.budget.enabled else None,
        )
        return loop

    @app.get("/api/status")
    async def get_status():
        from markbot import __version__
        return JSONResponse({
            "version": __version__,
            "token": _session_token,
        })

    @app.get("/api/sessions")
    async def list_sessions():
        sessions = []
        for sid, data in sorted(
            _chat_sessions.items(), key=lambda x: x[1].get("last_active", 0), reverse=True
        ):
            msgs = data.get("messages", [])
            if not msgs:
                continue
            preview = ""
            for m in reversed(msgs):
                if m.get("role") == "user" and m.get("content"):
                    preview = m["content"][:80]
                    break
            sessions.append({
                "id": sid,
                "title": data.get("title", preview or "新对话"),
                "message_count": len(msgs),
                "created_at": data.get("created_at", 0),
                "last_active": data.get("last_active", 0),
            })
        return JSONResponse({"sessions": sessions})

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        data = _chat_sessions.get(session_id)
        if not data:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return JSONResponse({
            "id": session_id,
            "title": data.get("title", "新对话"),
            "messages": data.get("messages", []),
            "created_at": data.get("created_at", 0),
            "last_active": data.get("last_active", 0),
        })

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        _chat_sessions.pop(session_id, None)
        return JSONResponse({"ok": True})

    async def ws_chat_handler(websocket: WebSocket):
        token = websocket.query_params.get("token", "")
        if token != _session_token:
            _log.warning("WebSocket rejected: invalid token")
            await websocket.close(code=4401)
            return

        await websocket.accept()
        _log.info("WebSocket accepted")
        agent = await _get_agent()

        session_id: str | None = None
        session_key: str | None = None

        def _ensure_session():
            nonlocal session_id, session_key
            if session_id is None:
                session_id = secrets.token_urlsafe(8)
                session_key = f"web:{session_id}"
                _chat_sessions[session_id] = {
                    "title": "新对话",
                    "messages": [],
                    "created_at": time.time(),
                    "last_active": time.time(),
                }

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"content": raw}

                msg_type = data.get("type", "")

                if msg_type == "new_session":
                    session_id = None
                    session_key = None
                    await websocket.send_text(json.dumps({
                        "type": "session_cleared",
                    }))
                    continue

                if msg_type == "resume_session":
                    target = data.get("session_id", "")
                    if target and target in _chat_sessions:
                        session_id = target
                        session_key = f"web:{session_id}"
                        await websocket.send_text(json.dumps({
                            "type": "session",
                            "session_id": session_id,
                        }))
                    continue

                user_content = data.get("content", "").strip()
                if not user_content:
                    continue

                _ensure_session()

                _chat_sessions[session_id]["messages"].append({
                    "role": "user",
                    "content": user_content,
                    "timestamp": time.time(),
                })
                _chat_sessions[session_id]["last_active"] = time.time()

                if len(_chat_sessions[session_id]["messages"]) == 1:
                    _chat_sessions[session_id]["title"] = user_content[:60]

                await websocket.send_text(json.dumps({
                    "type": "session",
                    "session_id": session_id,
                }))

                collected_chunks: list[str] = []

                async def on_stream(delta: str):
                    collected_chunks.append(delta)
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "stream_delta",
                            "delta": delta,
                        }))
                    except Exception:
                        pass

                async def on_progress(text: str):
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "progress",
                            "content": text,
                        }))
                    except Exception:
                        pass

                async def on_tool_start(tool_id: str, name: str, context: str | None):
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_start",
                            "tool_id": tool_id,
                            "name": name,
                            "context": context,
                        }))
                    except Exception:
                        pass

                async def on_tool_complete(
                    tool_id: str, name: str, summary: str | None, error: str | None
                ):
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_complete",
                            "tool_id": tool_id,
                            "name": name,
                            "summary": summary,
                            "error": error,
                        }))
                    except Exception:
                        pass

                try:
                    response = await agent.process_direct(
                        user_content,
                        session_key=session_key,
                        channel="web",
                        chat_id=session_id,
                        on_progress=on_progress,
                        on_stream=on_stream,
                        on_tool_start=on_tool_start,
                        on_tool_complete=on_tool_complete,
                    )

                    if collected_chunks:
                        full_content = "".join(collected_chunks)
                        _chat_sessions[session_id]["messages"].append({
                            "role": "assistant",
                            "content": full_content,
                            "timestamp": time.time(),
                        })
                        await websocket.send_text(json.dumps({
                            "type": "stream_end",
                        }))
                    else:
                        content = response.content if response else ""
                        _chat_sessions[session_id]["messages"].append({
                            "role": "assistant",
                            "content": content,
                            "timestamp": time.time(),
                        })
                        await websocket.send_text(json.dumps({
                            "type": "message",
                            "content": content,
                            "metadata": response.metadata if response else {},
                        }))

                    _chat_sessions[session_id]["last_active"] = time.time()

                except Exception as e:
                    _log.exception("Error processing message")
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "content": str(e),
                    }))

        except Exception:
            _log.info("WebSocket disconnected")

    app.routes.insert(0, WebSocketRoute("/api/ws/chat", endpoint=ws_chat_handler))

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="static-assets")

        @app.get("/")
        async def serve_index():
            from starlette.responses import Response as StarletteResponse
            content = (WEB_DIST / "index.html").read_bytes()
            return StarletteResponse(
                content,
                media_type="text/html",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        @app.get("/favicon.ico")
        async def serve_favicon():
            favicon = WEB_DIST / "favicon.ico"
            if favicon.exists():
                return FileResponse(favicon)
            return Response(status_code=204)
    else:
        @app.get("/")
        async def no_frontend():
            return HTMLResponse(
                "<h1>Markbot Web UI</h1>"
                "<p>Frontend not built. Run: <code>cd web && npm run build</code></p>"
            )

    return app


def start_server(host: str = "127.0.0.1", port: int = 9120):
    """Start the Markbot web server."""
    import uvicorn

    app = _build_app()

    print(f"\n  Markbot Web UI")
    print(f"  Listening on http://{host}:{port}")
    print(f"  Press Ctrl+C to stop\n")

    uvicorn.run(app, host=host, port=port, log_level="info")
