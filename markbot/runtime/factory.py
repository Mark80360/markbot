"""Declarative AgentLoop / side-service assembly.

Three product entrypoints (gateway, web, CLI) share the same AgentLoop
constructor surface but enable different side services.  Historically each
entrypoint pasted a 20+ parameter ``AgentLoop(...)`` block and slightly
different cron wiring — easy to drift.

This module is the single place that:

1. Builds ``MessageBus`` + provider + ``AgentLoop`` from config
2. Optionally creates ``CronService`` and wires job/failure callbacks
3. Optionally creates ``ChannelManager`` / ``HeartbeatService``
4. Exposes start/stop helpers for background services that belong to a profile

Process lifecycle (daemon pid, uvicorn, interactive TTY) stays in the
entrypoints — only *assembly and wiring* is unified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from markbot.bus.queue import MessageBus
from markbot.config.paths import get_cron_dir
from markbot.config.schema import Config
from markbot.types.permission import PermissionMode

if TYPE_CHECKING:
    from markbot.agent.loop import AgentLoop
    from markbot.channels.manager import ChannelManager
    from markbot.providers.fallback import FallbackManager
    from markbot.schedule.cron import CronService
    from markbot.schedule.dream import DreamService
    from markbot.schedule.heartbeat import HeartbeatService
    from markbot.session.session import SessionManager
    from markbot.skills.curator import CuratorService


# ---------------------------------------------------------------------------
# Feature matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeFeatures:
    """Capability flags for an entrypoint profile.

    Keep this declarative: entrypoints choose a profile; they should not
    re-implement cron/heartbeat wiring.
    """

    # Inject CronService into AgentLoop so cron tools can read/write jobs.
    cron_store: bool = True
    # Start the cron timer and wire on_job / on_failure.
    cron_runner: bool = False
    # On successful job: evaluate + publish_outbound when deliver is set.
    cron_deliver: bool = False
    # On exhausted retries: publish_outbound failure notice (else log only).
    cron_notify_failure: bool = False
    # Default channel/chat when job payload omits them.
    cron_default_channel: str = "cli"
    # After each cron job, trim the cron session history.
    cron_retain_session: bool = False
    cron_retain_messages: int = 8

    # Create ChannelManager (does not start it).
    channels: bool = False
    # Create HeartbeatService (does not start it).
    heartbeat: bool = False
    # Allow DreamService construction via runtime.create_dream().
    dream: bool = False
    # Allow CuratorService construction via runtime.create_curator().
    curator: bool = False
    # Pass an explicit SessionManager into AgentLoop (gateway/web).
    session_manager: bool = True


# Canonical profiles — entrypoints should import these rather than inventing
# ad-hoc flag combinations.
GATEWAY_FEATURES = RuntimeFeatures(
    cron_store=True,
    cron_runner=True,
    cron_deliver=True,
    cron_notify_failure=True,
    cron_default_channel="cli",
    cron_retain_session=True,
    channels=True,
    heartbeat=True,
    dream=True,
    curator=True,
    session_manager=True,
)

WEB_FEATURES = RuntimeFeatures(
    cron_store=True,
    cron_runner=True,
    cron_deliver=False,  # web process has no channel dispatcher by default
    cron_notify_failure=False,  # log only; no IM delivery path
    cron_default_channel="web",
    cron_retain_session=False,
    channels=False,
    heartbeat=False,
    dream=False,
    curator=False,
    session_manager=True,
)

CLI_FEATURES = RuntimeFeatures(
    cron_store=True,  # tools may write jobs.json
    cron_runner=False,  # CLI process does not run the timer
    cron_deliver=False,
    cron_notify_failure=False,
    cron_default_channel="cli",
    cron_retain_session=False,
    channels=False,
    heartbeat=False,
    dream=False,
    curator=False,
    session_manager=False,  # AgentLoop creates its own when None
)


# ---------------------------------------------------------------------------
# Runtime handle
# ---------------------------------------------------------------------------


@dataclass
class AgentRuntime:
    """Assembled runtime graph for one process entrypoint."""

    config: Config
    features: RuntimeFeatures
    bus: MessageBus
    provider: Any  # FallbackManager
    agent: Any  # AgentLoop
    cron: Any | None = None  # CronService | None
    channels: Any | None = None  # ChannelManager | None
    sessions: Any | None = None  # SessionManager | None
    heartbeat: Any | None = None  # HeartbeatService | None

    # Started later by entrypoints / start_background
    dream: Any | None = field(default=None, repr=False)
    curator: Any | None = field(default=None, repr=False)
    _cron_started: bool = field(default=False, repr=False)
    _heartbeat_started: bool = field(default=False, repr=False)

    # --- background lifecycle ------------------------------------------------

    async def start_cron(self) -> None:
        """Start the cron timer if this profile requested a runner."""
        if self.cron is None or not self.features.cron_runner:
            return
        if self._cron_started:
            return
        await self.cron.start()
        self._cron_started = True

    async def start_heartbeat(self) -> None:
        if self.heartbeat is None:
            return
        if self._heartbeat_started:
            return
        await self.heartbeat.start()
        self._heartbeat_started = True

    async def start_dream(self) -> Any | None:
        """Create and start DreamService when features.dream is enabled."""
        if not self.features.dream:
            return None
        if self.dream is not None:
            return self.dream
        dream_cron = self.config.tools.memory.dream_cron
        if not dream_cron or self.agent.memory_manager is None:
            return None
        from markbot.schedule.dream import DreamService

        self.dream = DreamService(
            cron_expr=dream_cron,
            dream_fn=self.agent.memory_manager.dream,
            state_dir=self.config.workspace_path,
            is_busy_fn=self.agent.has_active_conversations,
            timezone=self.config.agents.defaults.timezone,
        )
        await self.dream.start()
        return self.dream

    async def start_curator(self, *, interval_hours: int = 6) -> Any | None:
        if not self.features.curator:
            return None
        if self.curator is not None:
            return self.curator
        if self.agent.skill_registry is None:
            return None
        from markbot.skills.curator import CuratorService

        self.curator = CuratorService(
            workspace=self.config.workspace_path,
            skill_registry=self.agent.skill_registry,
            auto_archive=True,
            interval_hours=interval_hours,
        )
        await self.curator.start()
        return self.curator

    async def start_background(self) -> None:
        """Start cron + heartbeat + dream + curator according to features.

        ChannelManager and AgentLoop.run() remain the caller's responsibility
        (gateway races them against a restart sentinel).
        """
        await self.start_cron()
        await self.start_heartbeat()
        await self.start_dream()
        await self.start_curator()

    async def stop(self) -> None:
        """Stop side services and release agent MCP resources.

        Safe to call multiple times / partially started.
        """
        if self.dream is not None:
            try:
                await self.dream.stop()
            except Exception as e:
                logger.warning("Dream stop failed: {}", e)
            self.dream = None

        if self.curator is not None:
            try:
                await self.curator.stop()
            except Exception as e:
                logger.warning("Curator stop failed: {}", e)
            self.curator = None

        if self.heartbeat is not None and self._heartbeat_started:
            try:
                await self.heartbeat.stop()
            except Exception as e:
                logger.warning("Heartbeat stop failed: {}", e)
            self._heartbeat_started = False

        if self.cron is not None and self._cron_started:
            try:
                self.cron.stop()
            except Exception as e:
                logger.warning("Cron stop failed: {}", e)
            self._cron_started = False

        if self.channels is not None:
            try:
                await self.channels.stop_all()
            except Exception as e:
                logger.warning("Channels stop failed: {}", e)

        try:
            self.agent.stop()
        except Exception:
            pass
        try:
            await self.agent.close_mcp()
        except Exception as e:
            logger.warning("Agent MCP close failed: {}", e)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_runtime(
    config: Config,
    features: RuntimeFeatures,
    *,
    provider: Any | None = None,
    bus: MessageBus | None = None,
    make_provider: Callable[[Config], Any] | None = None,
) -> AgentRuntime:
    """Build a fully wired :class:`AgentRuntime` for *features*.

    Args:
        config: Loaded MarkBot config.
        features: Entrypoint capability profile.
        provider: Optional pre-built FallbackManager (avoids typer.Exit in tests).
        bus: Optional shared MessageBus.
        make_provider: Optional provider factory; defaults to
            ``markbot.cli.runtime.make_provider``.
    """
    from markbot.agent.loop import AgentLoop
    from markbot.session.session import SessionManager

    if bus is None:
        bus = MessageBus()

    if provider is None:
        if make_provider is None:
            from markbot.cli.runtime import make_provider as _default_make_provider

            make_provider = _default_make_provider
        provider = make_provider(config)

    sessions: SessionManager | None = None
    if features.session_manager:
        sessions = SessionManager(config.workspace_path)

    cron = None
    if features.cron_store or features.cron_runner:
        cron = _build_cron_service(config)

    agent = AgentLoop(
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
        session_manager=sessions,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
        compaction_config=config.compaction,
        max_budget_usd=config.budget.max_budget_usd if config.budget.enabled else None,
        warn_threshold_usd=config.budget.warn_threshold_usd,
        budget_config=config.budget if config.budget.enabled else None,
    )

    if cron is not None and features.cron_runner:
        _wire_cron_callbacks(cron, agent, bus, provider, features, config)

    channels = None
    if features.channels:
        from markbot.channels.manager import ChannelManager

        channels = ChannelManager(config, bus)

    heartbeat = None
    if features.heartbeat:
        heartbeat = _build_heartbeat(config, provider, agent, bus, sessions, channels)

    return AgentRuntime(
        config=config,
        features=features,
        bus=bus,
        provider=provider,
        agent=agent,
        cron=cron,
        channels=channels,
        sessions=sessions if sessions is not None else getattr(agent, "sessions", None),
        heartbeat=heartbeat,
    )


def _build_cron_service(config: Config):
    from markbot.schedule.cron import CronService

    cron_store_path = get_cron_dir(config.workspace_path) / "jobs.json"
    reliability = getattr(config, "reliability", None)
    return CronService(
        cron_store_path,
        max_retries=getattr(reliability, "cron_max_retries", 2),
        retry_delay_s=getattr(reliability, "cron_retry_delay_s", 5.0),
        dead_letter_keep=getattr(reliability, "dead_letter_keep", 50),
    )


def _wire_cron_callbacks(
    cron,
    agent,
    bus: MessageBus,
    provider,
    features: RuntimeFeatures,
    config: Config,
) -> None:
    """Attach on_job / on_failure according to the feature matrix."""
    from markbot.schedule.cron import CronJob

    reliability = getattr(config, "reliability", None)
    default_channel = features.cron_default_channel

    async def on_cron_failure(job: CronJob, error: str) -> None:
        if reliability is not None and not getattr(reliability, "notify_on_failure", True):
            return
        if not features.cron_notify_failure:
            logger.warning("Cron job '{}' failed after retries: {}", job.name, error)
            return
        channel = job.payload.channel or default_channel
        chat_id = job.payload.to or "direct"
        summary = (
            f"[Cron Failure] Job '{job.name}' failed after retries.\n"
            f"Error: {error}\n"
            f"Instruction: {job.payload.message[:300]}"
        )
        try:
            from markbot.bus.events import OutboundMessage

            await bus.publish_outbound(
                OutboundMessage(channel=channel, chat_id=chat_id, content=summary)
            )
        except Exception as exc:
            logger.warning("Failed to publish cron failure notice: {}", exc)

    async def on_cron_job(job: CronJob) -> str | None:
        from markbot.tools.cron import CronTool
        from markbot.tools.message import MessageTool

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            resp = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or default_channel,
                chat_id=job.payload.to or "direct",
                permission_mode=PermissionMode.AUTO,
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)
            if features.cron_retain_session:
                cron_session = agent.sessions.get_or_create(f"cron:{job.id}")
                cron_session.retain_recent_legal_suffix(features.cron_retain_messages)
                agent.sessions.save(cron_session)

        response = (resp.content if resp else "") or ""

        # Surface hard model/runtime failures so CronService can retry /
        # dead-letter instead of marking the job "ok" with an error string.
        failure_markers = (
            "All ",
            "error calling the AI model",
            "models in chain failed",
            "Budget exceeded",
            "budget exceeded",
        )
        lowered = response.lower()
        if (
            response.startswith("Sorry, I encountered an error")
            or any(m.lower() in lowered for m in failure_markers)
        ):
            raise RuntimeError(f"cron agent failure: {response[:500]}")

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if (
            features.cron_deliver
            and job.payload.deliver
            and job.payload.to
            and response
        ):
            from markbot.schedule.evaluator import evaluate_response

            should_notify = await evaluate_response(
                response, job.payload.message, provider, agent.model,
            )
            if should_notify:
                from markbot.bus.events import OutboundMessage

                await bus.publish_outbound(
                    OutboundMessage(
                        channel=job.payload.channel or default_channel,
                        chat_id=job.payload.to,
                        content=response,
                    )
                )
        return response

    cron.on_job = on_cron_job
    cron.on_failure = on_cron_failure


def _build_heartbeat(config, provider, agent, bus, sessions, channels):
    from markbot.schedule.heartbeat import HeartbeatService

    hb_cfg = config.gateway.heartbeat
    enabled_channels: set[str] = set()
    if channels is not None:
        enabled_channels = set(channels.enabled_channels)

    def _pick_heartbeat_target() -> tuple[str, str]:
        session_mgr = sessions or getattr(agent, "sessions", None)
        if session_mgr is not None:
            for item in session_mgr.list_sessions():
                key = item.get("key") or ""
                if ":" not in key:
                    continue
                channel, chat_id = key.split(":", 1)
                if channel in {"cli", "system"}:
                    continue
                if channel in enabled_channels and chat_id:
                    return channel, chat_id
        return "cli", "direct"

    async def on_heartbeat_execute(tasks: str) -> str:
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        resp = await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
            permission_mode=PermissionMode.AUTO,
        )

        session = agent.sessions.get_or_create("heartbeat")
        session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
        agent.sessions.save(session)
        return resp.content if resp else ""

    async def on_heartbeat_notify(response: str) -> None:
        from markbot.bus.events import OutboundMessage

        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return
        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response)
        )

    return HeartbeatService(
        workspace=config.workspace_path,
        fallback_manager=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        timezone=config.agents.defaults.timezone,
    )


__all__ = [
    "AgentRuntime",
    "CLI_FEATURES",
    "GATEWAY_FEATURES",
    "RuntimeFeatures",
    "WEB_FEATURES",
    "build_runtime",
]
