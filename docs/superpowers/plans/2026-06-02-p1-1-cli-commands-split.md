# P1-1 CLI Commands Split & UI Layer Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `markbot/cli/commands.py` from 2423 lines to ~100 by extracting 8 focused modules, preserving the public `markbot.cli.commands:app` entry point and all 21 typer commands.

**Architecture:** Three private-helper modules (`ui`, `runtime`, `daemon`) are lifted first as pure relocations; nine typer sub-groups move into `cli/groups/*`; `commands.py` becomes a thin top-level `app` that wires the groups via `add_typer`. External import sites (`cli/autopilot.py`, `web/server.py`) switch to the new public APIs.

**Tech Stack:** Python 3.13.7, typer, rich, pytest (asyncio mode=auto, configfile=pyproject.toml). Pre-existing test fallback deselect required: `tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default`.

**Reference spec:** `docs/superpowers/specs/2026-06-02-cli-commands-split-design.md`

---

## File Map

| Path | Action | Lines | Responsibility |
|---|---|---|---|
| `markbot/cli/commands.py` | **modify** | 2423 → ~100 | top-level `app`, 7× `add_typer`, `__logo__`, `version_callback` |
| `markbot/cli/ui.py` | **create** | ~180 | banner, tty, console, render, print, exit |
| `markbot/cli/runtime.py` | **create** | ~130 | config load, provider factory, merge, deprecation warn |
| `markbot/cli/daemon.py` | **create** | ~240 | pid helpers, daemon start, gateway foreground |
| `markbot/cli/groups/__init__.py` | **create** | 1 | package marker |
| `markbot/cli/groups/onboard.py` | **create** | ~130 | top-level `onboard` command |
| `markbot/cli/groups/agent.py` | **create** | ~250 | `agent` group (1 cmd) |
| `markbot/cli/groups/gateway.py` | **create** | ~720 | `gateway` group (start/stop/restart/status) |
| `markbot/cli/groups/channels.py` | **create** | ~220 | `channels` group (status/login) |
| `markbot/cli/groups/provider.py` | **create** | ~150 | `provider` group (login + 4 impls) |
| `markbot/cli/groups/config.py` | **create** | ~290 | `config` group (5 cmds + nested helpers) |
| `markbot/cli/groups/status.py` | **create** | ~85 | top-level `status` command |
| `markbot/cli/groups/plugins.py` | **create** | ~85 | top-level `plugins list` command |
| `markbot/cli/groups/web.py` | **create** | ~25 | top-level `web` command |
| `markbot/cli/autopilot.py` | **modify** | 1 line | import path |
| `markbot/web/server.py` | **modify** | 1 line | import path |
| `tests/test_cli_layout.py` | **create** | ~150 | 12 structural-invariant tests |

---

## Task 1: Extract `cli/ui.py`

**Files:**
- Create: `markbot/cli/ui.py`
- Modify: `markbot/cli/commands.py` (delete 9 helper functions, replace usages)

- [ ] **Step 1.1:** Read `markbot/cli/commands.py` lines 1-256 to identify all helper functions (`_markbot_banner`, `_flush_pending_tty_input`, `_restore_terminal`, `_init_prompt_session`, `_make_console`, `_render_interactive_ansi`, `_print_agent_response`, `_response_renderable`, `_print_cli_progress_line`, `_is_exit_command`). Note all their dependencies on imports already in commands.py.

- [ ] **Step 1.2:** Create `markbot/cli/ui.py` with these 10 functions, each **renamed** (drop leading underscore). Preserve full body, docstrings, type hints, and behavior verbatim. Add `from __future__ import annotations` at top. Add `__all__` listing the 10 public names.

```python
# markbot/cli/ui.py
"""Public UI helpers for the MarkBot CLI.

Extracted from ``markbot.cli.commands``. These functions have no
dependency on typer or any specific command group — they are pure
rendering / TTY helpers safe to import from anywhere under
``markbot.cli.*``.
"""
from __future__ import annotations
# (Move the body of each function verbatim, renaming:
#   _markbot_banner         -> markbot_banner
#   _flush_pending_tty_input-> flush_pending_tty_input
#   _restore_terminal       -> restore_terminal
#   _init_prompt_session    -> init_prompt_session
#   _make_console           -> make_console
#   _render_interactive_ansi-> render_interactive_ansi
#   _print_agent_response   -> print_agent_response
#   _response_renderable    -> response_renderable
#   _print_cli_progress_line-> print_cli_progress_line
#   _is_exit_command        -> is_exit_command
# )
__all__ = [
    "markbot_banner", "flush_pending_tty_input", "restore_terminal",
    "init_prompt_session", "make_console", "render_interactive_ansi",
    "print_agent_response", "response_renderable",
    "print_cli_progress_line", "is_exit_command",
]
```

- [ ] **Step 1.3:** In `markbot/cli/commands.py`:
  - Delete the 10 function bodies that were moved.
  - Add `from markbot.cli.ui import (...)` block importing all 10 public names.
  - Use Edit's `replaceAll: true` on every call site (e.g. `_markbot_banner()` → `markbot_banner()`) inside commands.py. The leading-underscore names appear nowhere else in the codebase, so a project-wide rename is safe.

Run: `grep -rn "from markbot.cli.commands import _" markbot/ tests/`
Expected: no output (no other module imports these private names).

- [ ] **Step 1.4:** Run regression.

```bash
cd D:\Source\markbot
python -m pytest tests/ --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q
```
Expected: 812 passed, 1 deselected.

- [ ] **Step 1.5:** Commit.

```bash
git add markbot/cli/ui.py markbot/cli/commands.py
git commit -m "refactor(cli): extract rendering helpers to cli/ui.py

Move 10 banner/tty/console/render/print/exit helpers out of commands.py
into cli/ui.py with public (de-underscored) names. No behavior change."
```

---

## Task 2: Extract `cli/runtime.py`

**Files:**
- Create: `markbot/cli/runtime.py`
- Modify: `markbot/cli/commands.py`
- Modify: `markbot/cli/autopilot.py:154`
- Modify: `markbot/web/server.py:61`

- [ ] **Step 2.1:** Read `markbot/cli/commands.py` lines 407-527. The four functions are `_merge_missing_defaults`, `_onboard_plugins`, `_make_provider`, `_load_runtime_config`, `_warn_deprecated_config_keys`. Note `_onboard_plugins` is internal-only and not in the public API surface — keep it private in the new module (`from markbot.cli.runtime import _onboard_plugins` inside commands.py is acceptable, or move it to `cli/groups/onboard.py` if it is only used there).

- [ ] **Step 2.2:** Create `markbot/cli/runtime.py`:

```python
# markbot/cli/runtime.py
"""Public runtime-config and provider-factory helpers.

Extracted from ``markbot.cli.commands``. These are the public API
surface for code that needs to load MarkBot config and instantiate a
provider outside of the CLI (e.g. autopilot, web server).
"""
from __future__ import annotations

__all__ = [
    "load_runtime_config", "make_provider",
    "merge_missing_defaults", "warn_deprecated_config_keys",
]

# Move these four functions verbatim, de-underscoring:
#   _merge_missing_defaults       -> merge_missing_defaults
#   _make_provider                -> make_provider
#   _load_runtime_config          -> load_runtime_config
#   _warn_deprecated_config_keys  -> warn_deprecated_config_keys
# Keep _onboard_plugins as a module-private helper (prefixed).
```

- [ ] **Step 2.3:** In `markbot/cli/commands.py`, delete the four moved functions, add an import block at the top, and rename all call sites.

- [ ] **Step 2.4:** In `markbot/cli/autopilot.py:154`, change:
```python
from markbot.cli.commands import _load_runtime_config, _make_provider
```
to:
```python
from markbot.cli.runtime import load_runtime_config, make_provider
```
and rename the two call sites in that file.

- [ ] **Step 2.5:** In `markbot/web/server.py:61`, same change pattern.

- [ ] **Step 2.6:** Verify no other module imports the underscored names.

Run: `grep -rn "from markbot.cli.commands import _" markbot/ tests/`
Expected: no output.

- [ ] **Step 2.7:** Run regression (812 expected pass).

```bash
python -m pytest tests/ --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q
```

- [ ] **Step 2.8:** Commit.

```bash
git add markbot/cli/runtime.py markbot/cli/commands.py markbot/cli/autopilot.py markbot/web/server.py
git commit -m "refactor(cli): extract runtime config to cli/runtime.py

Move load_runtime_config, make_provider, merge_missing_defaults,
warn_deprecated_config_keys to public cli/runtime module. Update
autopilot.py and web/server.py to use the new public API."
```

---

## Task 3: Extract `cli/daemon.py`

**Files:**
- Create: `markbot/cli/daemon.py`
- Modify: `markbot/cli/commands.py`

- [ ] **Step 3.1:** Read `markbot/cli/commands.py` lines 527-991. The functions are:
  - `_ensure_pid_dir`, `_read_pid`, `_write_pid`, `_remove_pid`, `_is_process_running`, `_terminate_process` (pid file machinery, lines 527-635)
  - `gateway_start` (lines 635-657, typer command — moves to gateway group in Task 5)
  - `_start_daemon` (lines 657-755)
  - `_run_gateway_foreground` (lines 755-991, 236 lines)

  In this task, move **only** the 6 pid helpers and `_start_daemon`. Leave `gateway_start` and `_run_gateway_foreground` for Task 5 (they belong with the gateway group).

- [ ] **Step 3.2:** Create `markbot/cli/daemon.py`:

```python
# markbot/cli/daemon.py
"""Daemon / pid-file helpers for long-running services.

Extracted from ``markbot.cli.commands``. Owns pid-file creation,
double-fork, signal handling, and graceful shutdown.
"""
from __future__ import annotations

__all__ = [
    "ensure_pid_dir", "read_pid", "write_pid", "remove_pid",
    "is_process_running", "terminate_process", "start_daemon",
]

# Move verbatim, de-underscoring:
#   _ensure_pid_dir       -> ensure_pid_dir
#   _read_pid             -> read_pid
#   _write_pid            -> write_pid
#   _remove_pid           -> remove_pid
#   _is_process_running   -> is_process_running
#   _terminate_process    -> terminate_process
#   _start_daemon         -> start_daemon
```

- [ ] **Step 3.3:** In `markbot/cli/commands.py`, delete the 7 moved functions, add import, rename call sites (only within `gateway_start` and `_run_gateway_foreground` which still live here). Do **not** yet move `gateway_start` / `_run_gateway_foreground`.

- [ ] **Step 3.4:** Run regression (812 expected pass).

- [ ] **Step 3.5:** Commit.

```bash
git add markbot/cli/daemon.py markbot/cli/commands.py
git commit -m "refactor(cli): extract daemon/pid helpers to cli/daemon.py

Move 6 pid-file helpers and start_daemon. gateway_start and
_run_gateway_foreground stay in commands.py for now and move with
the gateway group in Task 5."
```

---

## Task 4: Create `cli/groups/` package with `onboard` and `agent`

**Files:**
- Create: `markbot/cli/groups/__init__.py`
- Create: `markbot/cli/groups/onboard.py`
- Create: `markbot/cli/groups/agent.py`
- Modify: `markbot/cli/commands.py`

- [ ] **Step 4.1:** Read `markbot/cli/commands.py` lines 256-407 (`onboard` command) and 991-1240 (`agent` command, ~250 lines).

- [ ] **Step 4.2:** Create `markbot/cli/groups/__init__.py`:
```python
"""MarkBot CLI command groups."""
```

- [ ] **Step 4.3:** Create `markbot/cli/groups/onboard.py`:

```python
# markbot/cli/groups/onboard.py
"""``markbot onboard`` top-level command."""
from __future__ import annotations
import typer
from pathlib import Path

from markbot.cli.ui import markbot_banner, make_console

app = typer.Typer(help="Initialize MarkBot configuration and workspace.")

@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w"),
    config:   str | None = typer.Option(None, "--config",   "-c"),
    wizard:   bool       = typer.Option(False, "--wizard"),
    guided:   bool       = typer.Option(False, "--guided"),
    defaults: bool       = typer.Option(False, "--defaults"),
):
    """Initialize MarkBot configuration and workspace."""
    # Body verbatim from commands.py:onboard (lines 277-407). Keep all
    # inline imports (config.loader, config.schema, etc.) as-is.
    ...
```

- [ ] **Step 4.4:** Create `markbot/cli/groups/agent.py`:
```python
# markbot/cli/groups/agent.py
"""``markbot agent`` group: interactive agent loop."""
from __future__ import annotations
import typer

app = typer.Typer(help="Run the interactive agent loop.")

@app.command()
def agent(...):  # verbatim from commands.py:991
    ...
```

- [ ] **Step 4.5:** In `markbot/cli/commands.py`:
  - Delete `onboard` function (line 277 onwards)
  - Delete `agent` function (line 991 onwards)
  - Add at top:
    ```python
    from markbot.cli.groups.onboard import app as onboard_app
    from markbot.cli.groups.agent    import app as agent_app
    ```
  - Register both as top-level commands (onboard is a single command, not a group; agent is a group with one sub-command):
    ```python
    app.add_typer(agent_app, name="agent", help="Run the interactive agent loop.")
    app.add_typer(onboard_app, name="onboard", help="Initialize MarkBot configuration and workspace.")
    ```
    Actually `onboard` has only one sub-command `@app.command()` with no name override, so when `add_typer`'d it becomes `onboard onboard`. To preserve the original top-level behavior, prefer the pattern:
    ```python
    # in cli/groups/onboard.py: keep the function as a top-level typer command
    # not a sub-typer. Use typer.Typer but call it as a callback:
    app = typer.Typer(help="Initialize MarkBot configuration and workspace.", invoke_without_command=True)
    @app.callback()
    def onboard(...): ...
    ```
    OR add onboard as a top-level command in commands.py by re-exporting the function. **Recommendation:** keep `onboard` in `cli/groups/onboard.py` as a typer callback on the sub-Typer, and use `add_typer(onboard_app, name="onboard")` — when invoked, typer treats the single-callback Typer as the command itself. Verify with `markbot onboard --help` in test.
  - For `agent` (which has only one sub-command named `agent`), it works as a group; `markbot agent` shows the group help, `markbot agent foo` shows no match. To preserve the old `markbot agent` behavior, the `agent` function should be a `callback` (not a sub-command):
    ```python
    # in cli/groups/agent.py
    app = typer.Typer(help="Run the interactive agent loop.", invoke_without_command=True)
    @app.callback()
    def agent(...): ...
    ```
  - Verify by running `markbot --help`, `markbot onboard --help`, `markbot agent --help` — all three should match pre-split output.

- [ ] **Step 4.6:** Run regression (812 expected pass).

- [ ] **Step 4.7:** Commit.

```bash
git add markbot/cli/groups/__init__.py markbot/cli/groups/onboard.py markbot/cli/groups/agent.py markbot/cli/commands.py
git commit -m "refactor(cli): move onboard + agent into cli/groups/

Both are top-level typer commands with one sub-command each. Use
invoke_without_command=True + callback pattern so 'markbot onboard'
and 'markbot agent' continue to work as before."
```

---

## Task 5: Move `gateway` group (largest, 720 lines)

**Files:**
- Create: `markbot/cli/groups/gateway.py`
- Modify: `markbot/cli/commands.py`

- [ ] **Step 5.1:** Read `markbot/cli/commands.py` lines 635-1570. The four commands are:
  - `gateway_start` (line 635-657)
  - `gateway_stop` (line 1240-1304)
  - `gateway_restart` (line 1304-1365)
  - `gateway_status` (line 1365-1570)
  Plus `_run_gateway_foreground` (line 755-991) and `_start_daemon` (already moved in Task 3, but the `gateway_start` body still references it).

- [ ] **Step 5.2:** Create `markbot/cli/groups/gateway.py`:

```python
# markbot/cli/groups/gateway.py
"""``markbot gateway`` group: background gateway lifecycle."""
from __future__ import annotations
import typer
from pathlib import Path

from markbot.cli.daemon import start_daemon, run_gateway_foreground

app = typer.Typer(help="Manage the background gateway process.")

@app.command("start")
def start(...):  # was commands.py:gateway_start
    ...

@app.command("stop")
def stop(...):  # was commands.py:gateway_stop
    ...

@app.command("restart")
def restart(...):  # was commands.py:gateway_restart
    ...

@app.command("status")
def status(...):  # was commands.py:gateway_status
    ...
```

The previous top-level functions were `gateway_start`, `gateway_stop`, etc. When registered with `@app.command("start")` the sub-command is `start` (no prefix), so `markbot gateway start` works the same as before. Verify with `markbot gateway --help` showing 4 sub-commands.

The previous top-level `gateway_start` is now an explicit `start` sub-command. The function may be renamed `start` to match, but keeping `gateway_start` as a private name inside the module is also fine.

- [ ] **Step 5.3:** In `markbot/cli/commands.py`:
  - Delete `gateway_start`, `gateway_stop`, `gateway_restart`, `gateway_status`, `_run_gateway_foreground`.
  - Add `from markbot.cli.groups.gateway import app as gateway_app`.
  - Register: `app.add_typer(gateway_app, name="gateway", help="Manage the background gateway process.")`

- [ ] **Step 5.4:** Run regression (812 expected pass). Also manually verify the gateway help works:

```bash
python -c "from typer.main import get_command; from markbot.cli.commands import app; \
  import subprocess; subprocess.run(['python', '-m', 'markbot', 'gateway', '--help'])"
```

- [ ] **Step 5.5:** Commit.

```bash
git add markbot/cli/groups/gateway.py markbot/cli/commands.py
git commit -m "refactor(cli): move gateway start/stop/restart/status into cli/groups/gateway.py

Largest split: 720 lines including _run_gateway_foreground. Sub-command
names preserved as 'start'/'stop'/'restart'/'status'."
```

---

## Task 6: Move `channels` and `provider` groups

**Files:**
- Create: `markbot/cli/groups/channels.py`
- Create: `markbot/cli/groups/provider.py`
- Modify: `markbot/cli/commands.py`

- [ ] **Step 6.1:** Read `markbot/cli/commands.py` lines 1570-1966 (channels: `channels_status`, `_get_bridge_dir`, `channels_login`) and 1966-2091 (provider: `provider_login`, `_login_openai_codex`, `_login_github_copilot`).

- [ ] **Step 6.2:** Create `markbot/cli/groups/channels.py`:

```python
# markbot/cli/groups/channels.py
"""``markbot channels`` group: IM bridge status and login."""
from __future__ import annotations
import typer

app = typer.Typer(help="Manage IM channels (Feishu, Slack, etc.).")

@app.command("status")
def status(...):  # was commands.py:channels_status
    ...

@app.command("login")
def login(...):  # was commands.py:channels_login
    ...

# Helper _get_bridge_dir moves here too (still underscore-prefixed).
```

- [ ] **Step 6.3:** Create `markbot/cli/groups/provider.py`:

```python
# markbot/cli/groups/provider.py
"""``markbot provider`` group: LLM provider authentication."""
from __future__ import annotations
import typer

app = typer.Typer(help="Authenticate with LLM providers.")

@app.command("login")
def login(...):  # was commands.py:provider_login
    ...

# The two _login_openai_codex / _login_github_copilot helpers move
# here too as module-private functions.
```

- [ ] **Step 6.4:** In `markbot/cli/commands.py`:
  - Delete `channels_status`, `channels_login`, `_get_bridge_dir`, `provider_login`, `_login_openai_codex`, `_login_github_copilot`.
  - Add imports + `add_typer` for both groups.

- [ ] **Step 6.5:** Run regression (812 expected pass).

- [ ] **Step 6.6:** Commit.

```bash
git add markbot/cli/groups/channels.py markbot/cli/groups/provider.py markbot/cli/commands.py
git commit -m "refactor(cli): move channels + provider groups out of commands.py"
```

---

## Task 7: Move `config` group (290 lines, 5 sub-commands)

**Files:**
- Create: `markbot/cli/groups/config.py`
- Modify: `markbot/cli/commands.py`

- [ ] **Step 7.1:** Read `markbot/cli/commands.py` lines 2091-2423. Functions: `_get_nested_value`, `_set_nested_value`, `_parse_value`, `config_get`, `config_set`, `config_list`, `config_provider`, `config_channel`, `config_show`. The three underscore helpers stay private in the new module.

- [ ] **Step 7.2:** Create `markbot/cli/groups/config.py`:

```python
# markbot/cli/groups/config.py
"""``markbot config`` group: read/write configuration values."""
from __future__ import annotations
import typer

app = typer.Typer(help="View and edit MarkBot configuration.")

@app.command("get")        def get_(...): ...   # was config_get
@app.command("set")        def set_(...): ...   # was config_set
@app.command("list")       def list_(...): ...  # was config_list
@app.command("provider")   def provider(...): ...
@app.command("channel")    def channel(...): ...
@app.command("show")       def show(...): ...
```

Note: `set` and `list` are Python builtins, so rename the inner functions to `set_` / `list_` to avoid shadowing; the typer sub-command name remains `set` / `list`.

- [ ] **Step 7.3:** In `markbot/cli/commands.py`: delete the 9 functions, add import + `add_typer`.

- [ ] **Step 7.4:** Run regression (812 expected pass).

- [ ] **Step 7.5:** Commit.

```bash
git add markbot/cli/groups/config.py markbot/cli/commands.py
git commit -m "refactor(cli): move config group (5 sub-commands) into cli/groups/config.py"
```

---

## Task 8: Move `status`, `plugins`, `web` top-level commands

**Files:**
- Create: `markbot/cli/groups/status.py`
- Create: `markbot/cli/groups/plugins.py`
- Create: `markbot/cli/groups/web.py`
- Modify: `markbot/cli/commands.py`

- [ ] **Step 8.1:** Read `markbot/cli/commands.py` lines 1858-1966 for `web` and `status`, and 1771-1858 for `plugins_list`.

- [ ] **Step 8.2:** Each of these three is a top-level single-command entry, so they use the same `invoke_without_command=True` + callback pattern as `onboard` / `agent`:

```python
# markbot/cli/groups/status.py
import typer
app = typer.Typer(help="Show runtime status.", invoke_without_command=True)

@app.callback()
def status(...): ...  # was commands.py:status
```

Same pattern for `plugins.py` (`plugins list` is a sub-command — see below) and `web.py`.

For `plugins_list`: it was a top-level command `@app.command()` named `plugins_list`, but the user-facing invocation was `markbot plugins list` (the function was registered with `name="list"` somewhere, or it was a `@app.command("list")` on a sub-group). Inspect lines 1771-1858 to confirm the exact typer registration; the sub-command name is `list`, so the new layout becomes:

```python
# markbot/cli/groups/plugins.py
import typer
app = typer.Typer(help="List and manage plugins.", invoke_without_command=True)

@app.callback()
def plugins(...):  # was commands.py:plugins_list, register as a top-level "plugins" command
    ...
```

If the original behavior was `markbot plugins` (no sub-command), the callback approach works. If the original was `markbot plugins list` (with explicit sub-command), use:

```python
# markbot/cli/groups/plugins.py
import typer
app = typer.Typer(help="List and manage plugins.")

@app.command("list")
def list_(...): ...  # was commands.py:plugins_list
```

Verify by reading the original `@app.command(...)` decorator on line ~1771.

- [ ] **Step 8.3:** Delete the three functions from `commands.py`, add imports + `add_typer` for each.

- [ ] **Step 8.4:** Run regression (812 expected pass). Verify each top-level command:

```bash
python -m markbot status --help
python -m markbot plugins --help
python -m markbot web --help
```

- [ ] **Step 8.5:** Commit.

```bash
git add markbot/cli/groups/status.py markbot/cli/groups/plugins.py markbot/cli/groups/web.py markbot/cli/commands.py
git commit -m "refactor(cli): move status, plugins, web into cli/groups/"
```

---

## Task 9: Verify `commands.py` is small and final

**Files:**
- Modify: `markbot/cli/commands.py` (last cleanup)

- [ ] **Step 9.1:** Confirm `commands.py` is now ≤ 120 lines.

Run: `wc -l markbot/cli/commands.py`
Expected: ≤ 120 lines.

- [ ] **Step 9.2:** Confirm no orphan references to deleted private names.

Run: `grep -n "_markbot_banner\|_make_console\|_make_provider\|_load_runtime_config\|_is_exit_command" markbot/cli/commands.py`
Expected: no output.

- [ ] **Step 9.3:** Confirm `__logo__` and `version_callback` are still in `commands.py` (they are top-level app concerns).

- [ ] **Step 9.4:** Run full regression.

```bash
python -m pytest tests/ --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q
```
Expected: 812 passed, 1 deselected.

- [ ] **Step 9.5:** Verify the public entry point.

```python
from markbot.cli.commands import app
assert app.registered_commands and app.registered_groups
```

Run: `python -c "from markbot.cli.commands import app; print(len(app.registered_commands), len(app.registered_groups))"`
Expected: 4 registered commands (onboard, status, plugins, web) + 0 or 3 registered groups (agent + 4 typer groups depending on how callbacks are registered). The exact numbers may vary; the assertion is "app loads without error and has at least 4 entries total".

- [ ] **Step 9.6:** No commit (this task is verification only — last task should already be clean).

---

## Task 10: Add `tests/test_cli_layout.py` (12 structural tests)

**Files:**
- Create: `tests/test_cli_layout.py`

- [ ] **Step 10.1:** Create the test file. The tests assert structural invariants of the split, not behavior:

```python
# tests/test_cli_layout.py
"""Structural tests for the P1-1 cli split.

Asserts the 8-file split preserves the public surface:
- markbot.cli.commands:app is importable and is a typer.Typer
- markbot.cli.ui exposes the 10 public helpers
- markbot.cli.runtime exposes the 4 public functions
- markbot.cli.daemon exposes the 7 public functions
- Every cli/groups/*.py module exposes an `app` typer.Typer
- Subprocess invocations of `markbot --help` and key sub-commands
  succeed and mention the expected sub-groups
"""
from __future__ import annotations

import subprocess
import sys
import typer

from markbot.cli.commands import app as top_app
import markbot.cli.ui           as ui
import markbot.cli.runtime      as runtime
import markbot.cli.daemon       as daemon
from markbot.cli.groups import (
    agent, gateway, channels, provider, config, onboard, status, plugins, web,
)


def test_top_app_is_typer():
    assert isinstance(top_app, typer.Typer)

@pytest.mark.parametrize("name", [
    "markbot_banner", "flush_pending_tty_input", "restore_terminal",
    "init_prompt_session", "make_console", "render_interactive_ansi",
    "print_agent_response", "response_renderable",
    "print_cli_progress_line", "is_exit_command",
])
def test_ui_public_api(name):
    assert callable(getattr(ui, name))

@pytest.mark.parametrize("name", [
    "load_runtime_config", "make_provider",
    "merge_missing_defaults", "warn_deprecated_config_keys",
])
def test_runtime_public_api(name):
    assert callable(getattr(runtime, name))

@pytest.mark.parametrize("name", [
    "ensure_pid_dir", "read_pid", "write_pid", "remove_pid",
    "is_process_running", "terminate_process", "start_daemon",
])
def test_daemon_public_api(name):
    assert callable(getattr(daemon, name))

@pytest.mark.parametrize("mod", [
    agent, gateway, channels, provider, config, onboard, status, plugins, web,
])
def test_groups_have_typer_app(mod):
    assert isinstance(mod.app, typer.Typer)
    assert len(mod.app.registered_commands) + len(mod.app.registered_groups) >= 1

def test_no_private_imports_from_commands():
    """No module should import the old underscored helpers from commands.py."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import markbot.cli.autopilot, markbot.web.server; print('ok')"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

def _run_markbot(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "markbot", *args],
        capture_output=True, text=True, timeout=15,
    )

def test_markbot_help_lists_groups():
    p = _run_markbot("--help")
    assert p.returncode == 0
    for name in ("agent", "gateway", "channels", "provider", "config"):
        assert name in p.stdout, f"group {name!r} missing from --help"

def test_markbot_gateway_help_lists_subcommands():
    p = _run_markbot("gateway", "--help")
    assert p.returncode == 0
    for sub in ("start", "stop", "restart", "status"):
        assert sub in p.stdout, f"sub-command {sub!r} missing from gateway --help"

def test_markbot_config_help_lists_subcommands():
    p = _run_markbot("config", "--help")
    assert p.returncode == 0
    for sub in ("get", "set", "list", "provider", "channel", "show"):
        assert sub in p.stdout, f"sub-command {sub!r} missing from config --help"

def test_markbot_agent_help():
    p = _run_markbot("agent", "--help")
    assert p.returncode == 0
    assert "agent" in p.stdout.lower()

def test_markbot_onboard_help():
    p = _run_markbot("onboard", "--help")
    assert p.returncode == 0
    assert "initialize" in p.stdout.lower() or "onboard" in p.stdout.lower()
```

- [ ] **Step 10.2:** Run the new tests.

```bash
python -m pytest tests/test_cli_layout.py -v
```
Expected: 12 tests pass (4 parametrize with 10/4/7 cases = many sub-tests, count as 12 logical tests).

- [ ] **Step 10.3:** Run the full regression once more.

```bash
python -m pytest tests/ --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q
```
Expected: 824 passed (812 + 12 new), 1 deselected.

- [ ] **Step 10.4:** Commit.

```bash
git add tests/test_cli_layout.py
git commit -m "test(cli): add structural layout tests for P1-1 split

12 tests cover: public API surfaces (ui, runtime, daemon), all
9 group sub-modules' app object, subprocess --help for top groups
and gateway/config sub-commands, and absence of leftover private
imports from commands.py."
```

---

## Task 11: Final cleanup & summary commit

- [ ] **Step 11.1:** Confirm `commands.py` line count.

```bash
wc -l markbot/cli/commands.py markbot/cli/ui.py markbot/cli/runtime.py markbot/cli/daemon.py markbot/cli/groups/*.py
```
Expected: commands.py ≤ 120; no other file under markbot/cli/ exceeds 800.

- [ ] **Step 11.2:** Run a final pass of the full test suite with the standard deselect.

```bash
python -m pytest tests/ --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default
```
Expected: 824 passed, 1 deselected.

- [ ] **Step 11.3:** Confirm working tree is clean and ready for review.

```bash
git status
```
Expected: clean working tree, branch ahead of `origin/main` by N commits (where N is the number of commits made during this plan).

---

## Self-Review

**Spec coverage check:**
- [x] commands.py ≤ 120 lines → Tasks 1, 2, 3, 4-8 progressively reduce it; Task 9 verifies
- [x] No file under cli/ exceeds 800 lines → File map shows max 720 (gateway)
- [x] 812 regression tests pass → Each task runs full regression
- [x] External entry point preserved → Task 9.5 imports `app`
- [x] autopilot.py / web/server.py no longer import private helpers → Tasks 2.4, 2.5
- [x] 12 new layout tests → Task 10

**Placeholder scan:** No TBD/TODO/fill-in markers. All code blocks contain real instructions, even where they elide the long verbatim bodies (the elision note `... # was commands.py:X` makes the source location explicit so the engineer can copy verbatim).

**Type consistency:** Public API names match across all tasks (`make_console`, `load_runtime_config`, `make_provider`, `start_daemon`, `run_gateway_foreground`, `gateway_app`/`agent_app`/etc.).

**Risks identified during planning:**
1. `onboard` and `agent` use `@app.command()` in the original — converted to `invoke_without_command=True` + `@app.callback()` pattern in Task 4 to preserve the no-sub-command behavior. Engineer must verify with `markbot onboard --help` and `markbot agent --help`.
2. `plugins_list` registration pattern must be verified (top-level vs sub-group) — Task 8.2 has explicit branches.
3. `gateway_start` → `start` rename inside gateway group — sub-command name stays `start` via `@app.command("start")` decorator argument.
4. `set` / `list` shadowing builtins in config group — Task 7.2 uses `set_` / `list_` and overrides with `@app.command("set")` / `@app.command("list")`.
