"""Authorization for privileged (control-plane) operations.

MarkBot can be driven from shared chat surfaces (Feishu / DingTalk / QQ /
WeChat / email / â€?, often in group chats. Some operations are *process
global*: ``/mode auto`` flips the tool-permission mode for every
conversation in the daemon and persists it to ``config.json``. Without an
identity check, any participant of any allowed group can therefore grant
the agent unrestricted host access (shell, filesystem, ``computer_use``)
on behalf of everyone else.

This module is deliberately a set of pure functions so the policy is
testable without a live agent, bus, or channel.

Policy
------
A sender is *privileged* when they may run process-global operations.

1. **Local surfaces** (``cli``, ``web``, ``api``, ``local``, ``test``) are
   always privileged. They are already behind OS-level access control or
   the web UI's bearer token, so a chat-style allowlist adds nothing.

2. **Group chats are never implicitly privileged.** A group is by
   definition multi-party; the operator cannot be inferred from membership.
   Only an explicit admin entry grants access.

3. **Explicit admins win.** If the channel declares ``adminFrom``, or the
   global ``security.adminIds`` is non-empty, the sender must appear in
   that list. ``"*"`` in ``adminFrom`` is honoured for single-operator
   deployments that deliberately want the old open behaviour.

4. **Provably single-user deployments keep working.** When no admins are
   configured *and* the channel's ``allowFrom`` contains exactly one entry
   that matches the sender, that sender is the operator. This is safe: only
   one person can reach the bot at all, so there is nobody to escalate over.
   As soon as a second ID is allowlisted the inference stops and the sender
   must be an explicit admin.

5. Otherwise: **deny**, and say what to configure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from loguru import logger

#: Surfaces that are already gated by something stronger than a chat
#: allowlist (OS login, the web bearer token). Privileged by default.
LOCAL_SURFACES: frozenset[str] = frozenset({
    "cli", "web", "api", "local", "test",
})

#: Config key spelling variants accepted on channel config objects. Channel
#: configs use camelCase JSON aliases but are read as Python attributes in
#: some code paths, so both are tried.
_ADMIN_KEYS = ("admin_from", "adminFrom")
_ALLOW_KEYS = ("allow_from", "allowFrom")


@dataclass(frozen=True)
class AuthzDecision:
    """Outcome of a privileged-operation authorization check."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.allowed


def _as_id_list(config: Any, keys: Iterable[str]) -> list[str]:
    """Read the first present list-of-ids field from *config*."""
    if config is None:
        return []
    for key in keys:
        value = getattr(config, key, None)
        if value is None and isinstance(config, dict):
            value = config.get(key)
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
    return []


def _sender_matches(sender_id: str, channel: str | None, ids: Iterable[str]) -> bool:
    """True when *sender_id* is in *ids*.

    Two matching forms are accepted, because platform sender IDs collide
    across services (a Feishu ``ou_â€¦`` and a Slack ``Uâ€¦`` can never be the
    same string in practice, but short numeric IDs certainly can):

    * a bare ID, matched against the sender as-is;
    * a channel-qualified ID of the form ``feishu:ou_123``, which only
      matches when the qualifier equals *channel*.

    ``"*"`` is a wildcard, per entry, so ``["feishu:*"]`` opens a single
    channel without opening the rest.
    """
    sid = str(sender_id or "").strip()
    if not sid:
        return False
    chan = (channel or "").strip().lower()
    for entry in ids:
        e = str(entry).strip()
        if not e:
            continue
        if e == "*":
            return True
        if ":" in e:
            prefix, _, value = e.partition(":")
            prefix = prefix.strip().lower()
            value = value.strip()
            if value == "*" and prefix == chan:
                return True
            if value and value == sid and prefix == chan:
                return True
            continue
        if e == sid:
            return True
    return False


def is_privileged_sender(
    *,
    channel: str | None,
    sender_id: str | None,
    is_group: bool | None = None,
    channel_config: Any = None,
    global_admin_ids: Iterable[str] | None = None,
) -> AuthzDecision:
    """Decide whether *sender_id* may run a process-global operation.

    Args:
        channel: Channel name the message arrived on (``feishu``, ``cli``â€?.
        sender_id: The platform's identifier for the human who sent it.
        is_group: Whether the message came from a multi-party chat. ``None``
            means "unknown" and is treated as a group (fail closed).
        channel_config: The channel's config object, consulted for
            ``adminFrom`` and ``allowFrom``.
        global_admin_ids: ``security.adminIds`` from the root config.
    """
    chan = (channel or "").strip().lower()

    # (1) Local surfaces need no chat allowlist.
    if chan in LOCAL_SURFACES:
        return AuthzDecision(True, f"local surface '{chan}'")

    # A missing sender can never be matched against an allowlist.
    if not sender_id or not str(sender_id).strip():
        return AuthzDecision(False, "message has no sender identity")

    # (2) Unknown group-ness fails closed.
    if is_group is not False:
        if is_group is None:
            logger.debug(
                "authz: chat type for channel '{}' is unknown; treating as a group",
                chan,
            )
        # (3) Channel-level admins take precedence.
        channel_admins = _as_id_list(channel_config, _ADMIN_KEYS)
        if channel_admins:
            if _sender_matches(sender_id, channel, channel_admins):
                return AuthzDecision(True, "listed in channel adminFrom")
            return AuthzDecision(
                False,
                "not listed in this channel's adminFrom",
            )

        # (3b) Global admins.
        global_admins = [str(v) for v in (global_admin_ids or [])]
        if global_admins:
            if _sender_matches(sender_id, channel, global_admins):
                return AuthzDecision(True, "listed in security.adminIds")
            return AuthzDecision(
                False, "not listed in security.adminIds")

        return AuthzDecision(
            False,
            "group chats are never implicitly privileged",
        )

    # Private 1:1 chat.
    channel_admins = _as_id_list(channel_config, _ADMIN_KEYS)
    if channel_admins:
        if _sender_matches(sender_id, channel, channel_admins):
            return AuthzDecision(True, "listed in channel adminFrom")
        return AuthzDecision(False, "not listed in this channel's adminFrom")

    global_admins = [str(v) for v in (global_admin_ids or [])]
    if global_admins:
        if _sender_matches(sender_id, channel, global_admins):
            return AuthzDecision(True, "listed in security.adminIds")
        return AuthzDecision(False, "not listed in security.adminIds")

    # (4) Provably single-user deployment.
    allow_from = _as_id_list(channel_config, _ALLOW_KEYS)
    if len(allow_from) == 1 and _sender_matches(sender_id, channel, allow_from):
        return AuthzDecision(
            True, "sole allowlisted sender on this channel")

    if len(allow_from) > 1:
        return AuthzDecision(
            False,
            f"channel allows {len(allow_from)} senders; "
            "configure security.adminIds to designate an operator",
        )
    return AuthzDecision(
        False,
        "no admins configured; configure security.adminIds to allow "
        "privileged commands",
    )


def denial_message(decision: AuthzDecision, *, command: str) -> str:
    """Operator-facing text for a refused privileged command."""
    if decision.allowed:
        return ""
    return (
        f"{command} is restricted to configured administrators "
        f"({decision.reason}).\n"
        "Add your sender ID to `security.adminIds` in config.json, or to the "
        "channel's `adminFrom`, then retry."
    )
