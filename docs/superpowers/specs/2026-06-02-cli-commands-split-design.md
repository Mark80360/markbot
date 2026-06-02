# CLI Commands Split & UI Layer Extraction Design

Date: 2026-06-02
Status: Draft
Scope: `markbot/cli/commands.py` (2423 lines → ~100) + new `cli/{ui,runtime,daemon,groups/*}.py`

## Background

`markbot/cli/commands.py` is a 2423-line monolith containing:

- 21 typer-decorated entry points (top-level + 5 sub-groups)
- 180 lines of pure rendering / TTY helpers (banner, prompt, console factory, response rendering, progress line, exit detection)
- 130 lines of runtime-config loading (load, merge defaults, warn deprecations)
- 240 lines of daemon / pid-lock / foreground-loop machinery
- 5 sub-group command sets that share state but have no clear boundaries

Code review classified the resulting "god file" as a P1 maintainability risk. Linters, type-checkers, and reviewers all slow down on 2400-line files; on-call engineers hunting a `gateway start` bug scroll past 1500 lines of unrelated code. The pure-rendering helpers (no command logic) are the easiest win — they have no dependencies and can be lifted into `cli/ui.py` immediately, then consumed by every group.

## Approach: 8-file split, single PR, additive

1. Extract pure helpers to `cli/ui.py` (no behavior change, just relocation + de-underscore names).
2. Extract runtime config to `cli/runtime.py` (becomes the importable API for `cli/autopilot.py` and `web/server.py`).
3. Extract daemon primitives to `cli/daemon.py` (pid file read/write/lock + foreground loop).
4. Extract five command groups (`agent`, `gateway`, `channels`, `provider`, `config`) and three single-command groups (`onboard`, `status`, `web`, `plugins`) to `cli/groups/*.py`.
5. Reduce `cli/commands.py` to: `app = typer.Typer(...)`, `app.add_typer(...)` for each of the 7 groups, and the `__logo__` constant.
6. Update two external import sites (`cli/autopilot.py`, `web/server.py`) to use the new public APIs.

## Goals

- `cli/commands.py` ≤ 120 lines.
- No single file under `markbot/cli/` exceeds 800 lines.
- All 812 existing tests pass (with the pre-existing `test_circuit_threshold_default` deselect).
- External entry point `markbot.cli.commands:app` unchanged — `markbot --help` shows the same 7 top-level groups and identical sub-commands.
- `markbot/cli/autopilot.py` and `markbot/web/server.py` no longer import private helpers (`_load_runtime_config`, `_make_provider`) — they use the public `markbot.cli.runtime` module.

## Non-Goals

- No new commands, no new options, no behavior changes.
- No typer→click migration (markbot is firmly on typer).
- No refactor of `cli/onboard.py`, `cli/autopilot.py`, `cli/skills.py`, `cli/stream.py`, `cli/doctor.py`, `cli/slash_commands/*` — they stay as-is.
- No PII/secret handling changes (covered by P0-3).
- No file-lock / atomic-write changes (covered by P1-2).

---

## File Layout

```
markbot/cli/
├── commands.py          # ~100 lines: app + add_typer(7 groups) + __logo__
├── ui.py                # NEW   180 lines: banner/tty/console/render/print/exit (9 public funcs)
├── runtime.py           # NEW   130 lines: load_runtime_config, make_provider, _merge_missing_defaults, _warn_deprecated_config_keys
├── daemon.py            # NEW   240 lines: pid helpers + start_daemon + _run_gateway_foreground
└── groups/
    ├── __init__.py      # 1 line
    ├── onboard.py       # NEW   130 lines
    ├── agent.py         # NEW   250 lines
    ├── gateway.py       # NEW   720 lines
    ├── channels.py      # NEW   220 lines
    ├── provider.py      # NEW   150 lines
    ├── config.py        # NEW   290 lines
    ├── status.py        # NEW    85 lines
    ├── plugins.py       # NEW    85 lines
    └── web.py           # NEW    25 lines
```

## Module APIs

### `cli/ui.py` (public, no underscore prefix)

```python
def markbot_banner() -> None
def init_prompt_session() -> PromptSession
def restore_terminal() -> None
def make_console() -> Console
def render_interactive_ansi(text: str) -> str
def print_agent_response(console: Console, text: str) -> None
def response_renderable(text: str) -> Renderable
def print_cli_progress_line(console: Console, line: str) -> None
def is_exit_command(text: str) -> bool
```

### `cli/runtime.py` (public)

```python
def load_runtime_config(...): ...    # was _load_runtime_config
def make_provider(...): ...          # was _make_provider
def merge_missing_defaults(...): ... # was _merge_missing_defaults (de-underscored)
def warn_deprecated_config_keys(...): ...  # was _warn_deprecated_config_keys
```

### `cli/daemon.py` (public)

```python
def ensure_pid_dir(path: Path) -> None
def read_pid(path: Path) -> int | None
def write_pid(path: Path, pid: int) -> None
def remove_pid(path: Path) -> None
def is_process_running(pid: int) -> bool
def terminate_process(pid: int, timeout: float = 5.0) -> bool
def start_daemon(target: Callable[[], None]) -> None   # was _start_daemon
async def run_gateway_foreground(...) -> None          # was _run_gateway_foreground
```

### `cli/groups/*.py` (each exposes `app = typer.Typer(...)`)

Each group file follows the same skeleton:

```python
# cli/groups/gateway.py
import typer
app = typer.Typer(help="...")

@app.command("start")
def start(...): ...

@app.command("stop")
def stop(...): ...
```

`cli/commands.py` then wires them in with `app.add_typer`:

```python
from markbot.cli.groups import agent, gateway, channels, provider, config, onboard, status, plugins, web

def _register_groups(app: typer.Typer) -> None:
    app.add_typer(agent.app,    name="agent",    help="...")
    app.add_typer(gateway.app,  name="gateway",  help="...")
    # ... 5 more
```

## Migration of Cross-Module Imports

Before:
```python
# markbot/cli/autopilot.py:154
from markbot.cli.commands import _load_runtime_config, _make_provider
# markbot/web/server.py:61
from markbot.cli.commands import _make_provider
```

After:
```python
# markbot/cli/autopilot.py:154
from markbot.cli.runtime import load_runtime_config, make_provider
# markbot/web/server.py:61
from markbot.cli.runtime import make_provider
```

Internal: `cli/groups/*` modules import from `markbot.cli.ui`, `markbot.cli.runtime`, `markbot.cli.daemon` as needed.

## Testing

### A. Regression (existing 812 tests)

Run full suite with the pre-existing deselect:
```
python -m pytest tests/ \
    --deselect tests/test_fallback.py::TestFallbackManager::test_circuit_threshold_default -q
```

Expected: 812 passed, 1 deselected.

### B. New `tests/test_cli_layout.py` (~12 tests)

1. `from markbot.cli.commands import app` succeeds and `isinstance(app, typer.Typer)`.
2. `from markbot.cli.ui import markbot_banner, make_console, ...` — all 9 public symbols importable.
3. `from markbot.cli.runtime import load_runtime_config, make_provider` succeeds.
4. `from markbot.cli.daemon import start_daemon, run_gateway_foreground` succeeds.
5. `from markbot.cli.groups import agent, gateway, channels, provider, config, onboard, status, plugins, web` — every sub-module's `app` is a `typer.Typer` with at least one registered command.
6. `app.registered_groups` lists all 7 sub-group names.
7. `app.registered_commands` lists `onboard`, `status`, `plugins`, `web` (4 top-level commands).
8. `subprocess.run(["markbot", "--help"], capture_output=True)` exits 0 and stdout mentions all 7 sub-group names.
9. `subprocess.run(["markbot", "agent", "--help"], ...)` exits 0 and mentions the `agent` sub-command.
10. `subprocess.run(["markbot", "gateway", "--help"], ...)` lists `start`, `stop`, `restart`, `status`.
11. `subprocess.run(["markbot", "config", "--help"], ...)` lists `get`, `set`, `list`, `provider`, `channel`, `show`.
12. Round-trip import compatibility: `from markbot.cli.runtime import load_runtime_config, make_provider` is reachable from both `cli.autopilot` and `web.server` import chains without `ImportError`.

## Implementation Order (8 commits, atomic)

1. `refactor(cli): extract ui helpers to cli/ui.py` (new file, de-underscore, import in commands.py)
2. `refactor(cli): extract runtime config to cli/runtime.py` (new file, update commands.py imports)
3. `refactor(cli): extract daemon primitives to cli/daemon.py` (new file, update commands.py imports)
4. `refactor(cli): create cli/groups/ package with empty stubs`
5. `refactor(cli): move agent + onboard into cli/groups/`
6. `refactor(cli): move gateway into cli/groups/`
7. `refactor(cli): move channels/provider/config/status/plugins/web into cli/groups/`
8. `refactor(cli): update autopilot.py and web/server.py imports`

(Commits 5–7 can be combined into one if traffic is low; sequence above is the safest path. Final commit adds `tests/test_cli_layout.py`.)

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Typer decorator order is preserved | `add_typer` does not require command registration order; verified via test #6 |
| Subprocess `--help` differs after split | Test #8-#11 invoke real `markbot` binary and assert against expected names |
| `commands.py` cycles (groups import from each other) | Design rule: groups import from `ui/runtime/daemon` only, never from sibling groups |
| `onboard` is both a top-level command and a shared helper (`run_onboard`) | Top-level `onboard` command lives in `cli/groups/onboard.py`; existing `cli/onboard.py` (used by `channels_login` and `config_channel`) is untouched |
| `version_callback` referenced from `main` decorator | `version_callback` stays in `cli/commands.py` (it depends on `__logo__`); groups don't need it |
| Test count goes up but coverage doesn't | `test_cli_layout.py` tests structural invariants; full regression on existing 812 tests guards real behavior |

## Definition of Done

- [ ] All 8 new files exist; `cli/commands.py` ≤ 120 lines.
- [ ] `python -m pytest tests/ --deselect test_circuit_threshold_default` → 812 pass.
- [ ] `python -m pytest tests/test_cli_layout.py -v` → 12 new tests pass.
- [ ] `markbot --help` lists 7 sub-groups: `agent`, `gateway`, `channels`, `provider`, `config`, plus 4 top-level commands.
- [ ] `markbot agent --help`, `markbot gateway --help`, `markbot config --help` all exit 0 with expected sub-commands.
- [ ] `grep -r "from markbot.cli.commands import _" markbot/ tests/` returns no matches (no more private-symbol imports from commands.py).
- [ ] Working tree is clean; all commits pushed to local `main`.
