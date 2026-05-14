"""`markbot doctor` diagnostic checks and fixes.

Layout
------
Section 1 Read-only check functions (no config or disk mutations).
Section 2 Fix runner (conservative filesystem repairs with backups).
Section 3 CLI entry points (typer app, ``doctor`` and ``doctor fix``).
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import typer
from rich.console import Console
from rich.text import Text

from markbot import __logo__, __version__
from markbot.config.loader import get_config_path, load_config
from markbot.config.paths import get_cron_dir
from markbot.config.schema import Config, ProviderConfig

console = Console()

# Section 1 Read-only checks


def environment_summary_lines() -> list[str]:
    py_ver = sys.version.split()[0]
    lines = [
        f"python version: {py_ver}",
        f"markbot version: {__version__}",
        f"platform: {platform.system()} {platform.release()} {platform.machine()}",
        f"python executable: {sys.executable}",
    ]
    try:
        import psutil
        lines.append(f"psutil: available ({psutil.__version__})")
    except ImportError:
        lines.append("psutil: not installed (optional; pip install psutil)")

    lines.append(f"sqlite library: {sqlite3.sqlite_version}")

    config_path = get_config_path()
    lines.append(f"config path: {config_path}")
    lines.append(f"config exists: {'yes' if config_path.is_file() else 'no'}")

    return lines


def check_config_file() -> tuple[bool, str]:
    config_path = get_config_path()

    if not config_path.is_file():
        return False, f"config file not found: {config_path}"

    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"invalid JSON in {config_path}: {e}"
    except OSError as e:
        return False, f"cannot read {config_path}: {e}"

    try:
        Config.model_validate(data)
    except Exception as e:
        return False, f"schema validation failed: {e}"

    return True, f"{config_path}"


def check_model_chain(cfg: Config) -> tuple[bool, str]:
    errors = cfg.validate_model_chain()
    if errors:
        return False, "\n".join(errors)

    chain = cfg.agents.defaults.model_chain
    if not chain:
        return False, "model_chain is empty no LLM configured"

    return True, f"{len(chain)} model(s): {', '.join(chain)}"


def check_primary_provider_configured(cfg: Config) -> tuple[bool, str]:
    chain = cfg.agents.defaults.model_chain
    if not chain:
        return False, "no model in chain"

    ref = chain[0]
    try:
        provider_cfg, model_cfg = cfg.resolve_model(ref)
    except ValueError as e:
        return False, str(e)

    provider_name = ref.split("/", 1)[0]

    from markbot.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    is_local = spec.is_local if spec else False
    is_direct = spec.is_direct if spec else False
    is_oauth = spec.is_oauth if spec else False

    if is_oauth:
        return True, f"{provider_name} (OAuth-based, no API key needed)"

    if is_local:
        if provider_cfg.api_base or spec and spec.default_api_base:
            return True, f"{provider_name} (local, api_base configured)"
        return True, f"{provider_name} (local)"

    if is_direct:
        if not provider_cfg.api_base and not (spec and spec.default_api_base):
            return False, f"{provider_name}: api_base is required for direct providers"
        return True, f"{provider_name} (direct, api_base configured)"

    if not provider_cfg.api_key:
        env_key = spec.env_key if spec else ""
        if env_key:
            env_val = os.getenv(env_key, "")
            if env_val:
                return True, f"{provider_name} (API key from env {env_key})"
            return False, f"{provider_name}: API key not set (config or env {env_key})"
        return False, f"{provider_name}: API key not configured"

    return True, f"{provider_name} (API key in config)"


def check_all_providers_credentials(cfg: Config) -> list[str]:
    notes: list[str] = []

    for field_name in cfg.providers.model_fields:
        provider_cfg = cfg.providers.get_provider(field_name)
        if provider_cfg is None:
            provider_cfg = getattr(cfg.providers, field_name, None)
        if not isinstance(provider_cfg, ProviderConfig):
            continue
        if not provider_cfg.models:
            continue
        if not provider_cfg.api_key and not provider_cfg.api_base:
            from markbot.providers.registry import find_by_name

            spec = find_by_name(field_name)
            is_local = spec.is_local if spec else False
            is_direct = spec.is_direct if spec else False
            is_oauth = spec.is_oauth if spec else False

            if is_oauth or is_local:
                continue

            env_key = spec.env_key if spec else ""
            if env_key:
                env_val = os.getenv(env_key, "")
                if env_val:
                    continue

            notes.append(
                f"{field_name}: has {len(provider_cfg.models)} model(s) but no API key "
                f"(set config or env {env_key})"
            )

    return notes


def check_workspace(cfg: Config) -> tuple[bool, str]:
    ws = cfg.workspace_path

    if not ws.exists():
        parent = ws.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return True, f"{ws} (not yet created; parent is writable)"
        return False, f"workspace directory does not exist: {ws}"

    if not ws.is_dir():
        return False, f"workspace path is not a directory: {ws}"

    if not os.access(ws, os.W_OK):
        return False, f"workspace directory is not writable: {ws}"

    return True, str(ws)


def check_workspace_subdirs(cfg: Config) -> list[str]:
    notes: list[str] = []
    ws = cfg.workspace_path

    for subdir in ["memory", "sessions", "cron"]:
        path = ws / subdir
        if path.exists() and not os.access(path, os.W_OK):
            notes.append(f"workspace/{subdir} exists but is not writable")

    return notes


def check_workspace_disk_space(cfg: Config) -> list[str]:
    notes: list[str] = []
    ws = cfg.workspace_path

    try:
        anchor = ws if ws.exists() else ws.parent
        if not anchor.exists():
            return notes
        du = shutil.disk_usage(anchor)
        free_gib = du.free / (1024**3)
        if free_gib < 0.5:
            notes.append(
                f"very low disk space on workspace volume: {free_gib:.2f} GiB free"
            )
        elif free_gib < 2.0:
            notes.append(
                f"low disk space on workspace volume: {free_gib:.2f} GiB free"
            )
    except OSError:
        pass

    return notes


def check_channels(cfg: Config) -> tuple[bool, str]:
    enabled: list[str] = []
    warnings: list[str] = []

    from loguru import logger

    logger.disable("markbot.channels.discovery")

    try:
        from markbot.channels.discovery import discover_all

        channel_classes = discover_all()
    finally:
        logger.enable("markbot.channels.discovery")

    for name in cfg.channels.model_fields:
        section = getattr(cfg.channels, name, None)
        if section is None:
            continue

        if isinstance(section, dict):
            is_enabled = section.get("enabled", False)
        elif hasattr(section, "enabled"):
            is_enabled = getattr(section, "enabled", False)
        else:
            is_enabled = False

        if not is_enabled:
            continue

        enabled.append(name)

        if name not in channel_classes:
            warnings.append(f"{name}: enabled but no channel class found (plugin missing?)")

    if not enabled:
        return True, "no channels enabled (CLI mode only)"

    detail = f"{len(enabled)} enabled: {', '.join(enabled)}"
    if warnings:
        return False, f"{detail}\n" + "\n".join(warnings)

    return True, detail


def check_channel_credentials(cfg: Config) -> list[str]:
    notes: list[str] = []

    for name in cfg.channels.model_fields:
        section = getattr(cfg.channels, name, None)
        if section is None:
            continue

        if isinstance(section, dict):
            is_enabled = section.get("enabled", False)
        elif hasattr(section, "enabled"):
            is_enabled = getattr(section, "enabled", False)
        else:
            is_enabled = False

        if not is_enabled:
            continue

        if isinstance(section, dict):
            for key in ("app_id", "app_secret", "token", "api_key", "api_secret"):
                val = section.get(key, "")
                if val and isinstance(val, str) and not val.strip():
                    notes.append(f"{name}.{key} is set but empty")
        else:
            for attr in ("app_id", "app_secret", "token", "api_key", "api_secret"):
                val = getattr(section, attr, None)
                if val is not None and isinstance(val, str) and not val.strip():
                    notes.append(f"{name}.{attr} is set but empty")

    return notes


def check_mcp_servers(cfg: Config) -> list[str]:
    notes: list[str] = []

    for name, mcp in cfg.tools.mcp_servers.items():
        if mcp.type == "stdio" or (not mcp.type and mcp.command):
            if not mcp.command:
                notes.append(f"mcp.{name}: stdio type but no command specified")
        elif mcp.type in ("sse", "streamableHttp") or (not mcp.type and mcp.url):
            if not mcp.url:
                notes.append(f"mcp.{name}: {mcp.type} type but no url specified")
        elif not mcp.command and not mcp.url:
            notes.append(f"mcp.{name}: no command or url specified")

    return notes


def check_skills(cfg: Config) -> tuple[bool, str]:
    from markbot.skills.core.loader import SkillLoader

    try:
        loader = SkillLoader(cfg.workspace_path)
        skills = loader.load_all()
        builtin = sum(1 for s in skills if s.is_builtin)
        workspace = sum(1 for s in skills if not s.is_builtin)
        return True, f"{len(skills)} skill(s): {builtin} builtin, {workspace} workspace"
    except Exception as e:
        return False, f"skill loading error: {e}"


def check_memory_config(cfg: Config) -> list[str]:
    notes: list[str] = []

    mem = cfg.tools.memory
    if mem.embedding_backend == "openai":
        if not mem.embedding_api_key:
            env_key = os.getenv("OPENAI_API_KEY", "")
            if not env_key:
                notes.append(
                    "embedding_backend is 'openai' but no embedding_api_key "
                    "or OPENAI_API_KEY env var set"
                )
        if not mem.embedding_model_name:
            notes.append(
                "embedding_backend is 'openai' but embedding_model_name not set "
                "(will use default)"
            )
    elif mem.embedding_backend == "ollama":
        if not mem.embedding_base_url:
            notes.append(
                "embedding_backend is 'ollama' but embedding_base_url not set "
                "(default: http://localhost:11434)"
            )

    return notes


def check_cron_jobs(cfg: Config) -> tuple[bool, str]:
    cron_dir = get_cron_dir(cfg.workspace_path)
    jobs_file = cron_dir / "jobs.json"

    if not jobs_file.exists():
        return True, "no jobs.json (cron not configured)"

    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return False, f"jobs.json: invalid JSON: {e}"
    except OSError as e:
        return False, f"jobs.json: read error: {e}"

    if not isinstance(data, dict):
        return False, "jobs.json: root must be a JSON object"

    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        return False, "jobs.json: 'jobs' must be a list"

    enabled = sum(1 for j in jobs if isinstance(j, dict) and j.get("enabled", True))
    return True, f"{len(jobs)} job(s), {enabled} enabled"


def check_gateway_pid() -> tuple[bool, str]:
    pid_file = Path.home() / ".markbot" / "gateway" / "gateway.pid"

    if not pid_file.exists():
        return True, "not running (no PID file)"

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return False, f"invalid PID file: {pid_file}"

    try:
        import psutil
        if psutil.pid_exists(pid):
            try:
                proc = psutil.Process(pid)
                cmdline = " ".join(proc.cmdline())
                if "markbot" in cmdline.lower() or "gateway" in cmdline.lower():
                    return True, f"running (PID {pid})"
                return False, f"PID {pid} exists but is not markbot gateway"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False, f"PID {pid} not accessible (stale PID file?)"
        return False, f"PID {pid} not running (stale PID file)"
    except ImportError:
        try:
            os.kill(pid, 0)
            return True, f"running (PID {pid}) [psutil not installed]"
        except ProcessLookupError:
            return False, f"PID {pid} not running (stale PID file)"
        except PermissionError:
            return True, f"running (PID {pid}) [psutil not installed]"


def check_gateway_log() -> tuple[bool, str]:
    log_file = Path.home() / ".markbot" / "gateway" / "gateway.log"

    if not log_file.exists():
        return True, "no log file (gateway not started yet)"

    try:
        size_kb = log_file.stat().st_size / 1024
        return True, f"{size_kb:.1f} KB"
    except OSError as e:
        return False, f"cannot stat log file: {e}"


def check_sessions(cfg: Config) -> tuple[bool, str]:
    sessions_dir = cfg.workspace_path / "sessions"

    if not sessions_dir.exists():
        return True, "no sessions directory (no conversations yet)"

    jsonl_files = list(sessions_dir.glob("*.jsonl"))
    total_size = sum(f.stat().st_size for f in jsonl_files if f.is_file()) / 1024

    return True, f"{len(jsonl_files)} session(s), {total_size:.0f} KB"


def check_config_unknown_keys() -> list[str]:
    config_path = get_config_path()
    if not config_path.is_file():
        return []

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []

    if not isinstance(raw, dict):
        return []

    known = set(Config.model_fields.keys())
    unknown = [k for k in raw if k not in known]
    return [f"unknown top-level key: {k!r}" for k in unknown]


def check_data_dir_writable() -> tuple[bool, str]:
    data_dir = get_config_path().parent

    if not data_dir.exists():
        parent = data_dir.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return True, f"{data_dir} (not yet created; parent is writable)"
        return False, f"data directory does not exist: {data_dir}"

    if not os.access(data_dir, os.W_OK):
        return False, f"data directory is not writable: {data_dir}"

    return True, str(data_dir)


def check_python_version() -> list[str]:
    notes: list[str] = []
    vi = sys.version_info

    if vi < (3, 10):
        notes.append(
            f"Python {vi.major}.{vi.minor} is below minimum 3.10 "
            "markbot requires Python 3.10+"
        )

    return notes


def check_optional_dependencies() -> list[str]:
    notes: list[str] = []

    optional = [
        ("psutil", "system monitoring in gateway status"),
        ("yaml", "YAML frontmatter in skills (PyYAML)"),
        ("markbot.memory.manager", "MemoryManager file-based memory"),
        ("chromadb", "vector storage for memory search"),
    ]

    for module, purpose in optional:
        try:
            __import__(module.replace("-", "_"))
        except ImportError:
            notes.append(f"{module} not installed (needed for {purpose})")

    return notes


# ---------------------------------------------------------------------------
# Section 2 Fix runner
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / ".markbot"
BACKUP_SUBDIR = "doctor-fix-backups"

SAFE_FIX_IDS = frozenset(
    {
        "ensure-data-dir",
        "ensure-workspace-dirs",
        "clean-stale-pid",
    }
)
RISKY_FIX_IDS = frozenset(
    {
        "seed-empty-jobs-json",
        "trim-gateway-log",
    }
)
ALL_FIX_IDS = SAFE_FIX_IDS | RISKY_FIX_IDS


def _utc_session_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(4)
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(8)}")
    try:
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _path_allowed(target: Path) -> bool:
    try:
        target.resolve().relative_to(DATA_DIR.resolve())
        return True
    except ValueError:
        return False


def _backup_one_file(session_files: Path, path: Path) -> None:
    rel = path.resolve().relative_to(DATA_DIR.resolve())
    dest = session_files / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, dest)


def _write_meta(
    session_dir: Path,
    argv: list[str],
    fix_ids: list[str],
    backed_paths: list[str],
    *,
    dry_run: bool,
    yes: bool,
    no_backup: bool,
) -> None:
    meta = {
        "markbot_version": __version__,
        "utc": datetime.now(timezone.utc).isoformat(),
        "argv": argv,
        "fix_ids": fix_ids,
        "backed_up_files_relative": backed_paths,
        "dry_run": dry_run,
        "yes": yes,
        "no_backup": no_backup,
        "restore_hint": (
            "Copy files from the 'files/' subtree back to ~/.markbot/ "
            "with the same relative paths."
        ),
    }
    (session_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class PlannedFix:
    fix_id: str
    description: str
    paths_to_backup: tuple[Path, ...]
    apply_fn: Callable[[], None]


def _parse_only(only: str | None) -> list[str]:
    if not only or not only.strip():
        return sorted(SAFE_FIX_IDS)
    ids = [x.strip() for x in only.split(",") if x.strip()]
    bad = [x for x in ids if x not in ALL_FIX_IDS]
    if bad:
        raise ValueError(
            f"unknown fix id(s): {bad!r}; known: {sorted(ALL_FIX_IDS)}"
        )
    return ids


def _try_load_config():
    try:
        ok, _ = check_config_file()
        if not ok:
            return None
        return load_config()
    except Exception:
        return None


def plan_fixes(
    fix_ids: list[str],
    yes: bool,
) -> tuple[list[str], list[PlannedFix]]:
    skip_msgs: list[str] = []
    planned: list[PlannedFix] = []

    for fid in fix_ids:
        if fid in RISKY_FIX_IDS and not yes:
            raise ValueError(
                f"fix {fid!r} requires --yes (-y) to apply "
                "(may modify files). "
                "Use `markbot doctor fix --dry-run --only ...` to preview."
            )

    if "ensure-data-dir" in fix_ids:
        if not DATA_DIR.is_dir():
            parent = DATA_DIR.parent
            if not parent.is_dir():
                raise ValueError(
                    f"refusing to create {DATA_DIR}: parent does not exist: {parent}"
                )

            def _mk(d: Path = DATA_DIR) -> None:
                d.mkdir(mode=0o700, exist_ok=False)

            planned.append(
                PlannedFix(
                    "ensure-data-dir",
                    f"create data directory {DATA_DIR}",
                    (),
                    _mk,
                )
            )
        else:
            skip_msgs.append("ensure-data-dir: already exists")

    if "ensure-workspace-dirs" in fix_ids:
        cfg = _try_load_config()
        if cfg is None:
            skip_msgs.append("ensure-workspace-dirs: skipped (config invalid)")
        else:
            ws = cfg.workspace_path
            subdirs = ["memory", "sessions", "cron", "skills", "media"]

            if not ws.is_dir():
                if _path_allowed(ws):

                    def _mk_ws(p: Path = ws) -> None:
                        p.mkdir(mode=0o700, parents=True, exist_ok=True)

                    planned.append(
                        PlannedFix(
                            "ensure-workspace-dirs",
                            f"create workspace directory {ws}",
                            (),
                            _mk_ws,
                        )
                    )
                else:
                    skip_msgs.append(f"ensure-workspace-dirs: {ws} outside data dir, skipped")

            for subdir in subdirs:
                sub = ws / subdir
                if not sub.is_dir():
                    if _path_allowed(sub):

                        def _mk_sub(p: Path = sub) -> None:
                            p.mkdir(mode=0o700, parents=True, exist_ok=True)

                        planned.append(
                            PlannedFix(
                                "ensure-workspace-dirs",
                                f"create {sub}",
                                (),
                                _mk_sub,
                            )
                        )

    if "clean-stale-pid" in fix_ids:
        pid_file = DATA_DIR / "gateway" / "gateway.pid"
        if pid_file.is_file():
            ok, detail = check_gateway_pid()
            if not ok and "stale" in detail.lower():

                def _rm_pid(pf: Path = pid_file) -> None:
                    pf.unlink()

                planned.append(
                    PlannedFix(
                        "clean-stale-pid",
                        f"remove stale PID file {pid_file}",
                        (pid_file,),
                        _rm_pid,
                    )
                )
            elif ok:
                skip_msgs.append("clean-stale-pid: gateway is running, nothing to clean")
            else:
                skip_msgs.append(f"clean-stale-pid: {detail}")
        else:
            skip_msgs.append("clean-stale-pid: no PID file found")

    if "seed-empty-jobs-json" in fix_ids:
        cfg = _try_load_config()
        if cfg is None:
            skip_msgs.append("seed-empty-jobs-json: skipped (config invalid)")
        else:
            cron_dir = get_cron_dir(cfg.workspace_path)
            jobs_file = cron_dir / "jobs.json"

            if jobs_file.exists():
                skip_msgs.append("seed-empty-jobs-json: jobs.json already exists")
            else:
                body = json.dumps({"version": 1, "jobs": []}, indent=2) + "\n"

                def _seed(p: Path = jobs_file) -> None:
                    if not _path_allowed(p):
                        raise RuntimeError(f"path not allowed: {p}")
                    _atomic_write_text(p, body)

                planned.append(
                    PlannedFix(
                        "seed-empty-jobs-json",
                        f"write empty {jobs_file}",
                        (),
                        _seed,
                    )
                )

    if "trim-gateway-log" in fix_ids:
        log_file = DATA_DIR / "gateway" / "gateway.log"
        if log_file.is_file():
            size_mb = log_file.stat().st_size / (1024 * 1024)
            if size_mb > 5.0:
                max_lines = 500

                def _trim(lf: Path = log_file, ml: int = max_lines) -> None:
                    if not _path_allowed(lf):
                        raise RuntimeError(f"path not allowed: {lf}")
                    try:
                        lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
                        if len(lines) > ml:
                            kept = lines[-ml:]
                            _atomic_write_text(lf, "\n".join(kept) + "\n")
                    except OSError:
                        pass

                planned.append(
                    PlannedFix(
                        "trim-gateway-log",
                        f"trim {log_file} ({size_mb:.1f} MB 鈫?keep last {max_lines} lines)",
                        (log_file,),
                        _trim,
                    )
                )
            else:
                skip_msgs.append(f"trim-gateway-log: log is {size_mb:.1f} MB, no trim needed")
        else:
            skip_msgs.append("trim-gateway-log: no log file found")

    return skip_msgs, planned


def run_doctor_fix(
    *,
    dry_run: bool,
    yes: bool,
    only: str | None,
    no_backup: bool,
    echo: Callable[[str], None],
    echo_err: Callable[[str], None],
    confirm_fn: Callable[[str], bool] | None = None,
    argv: list[str] | None = None,
) -> int:
    argv = argv if argv is not None else []

    try:
        fix_ids = _parse_only(only)
    except ValueError as exc:
        echo_err(str(exc))
        return 1

    try:
        skip_msgs, planned = plan_fixes(fix_ids, yes=yes)
    except ValueError as exc:
        echo_err(str(exc))
        return 1

    for msg in skip_msgs:
        echo(f"  (note) {msg}")

    if not planned:
        echo("Nothing to do (already satisfied).")
        return 0

    echo("Planned operations:")
    for p in planned:
        echo(f"  [{p.fix_id}] {p.description}")

    if dry_run:
        echo("(dry-run: no files modified)")
        return 0

    if no_backup:
        echo_err(
            "WARNING: --no-backup no copies under doctor-fix-backups "
            "if something goes wrong."
        )

    skip_confirm = yes
    if not skip_confirm:
        fn = confirm_fn or (lambda _m: False)
        if not fn("Apply these changes?"):
            echo("Aborted (no changes written).")
            return 0

    session_dir: Path | None = None
    session_files: Path | None = None
    backed_rels: list[str] = []

    if not no_backup:
        session_dir = DATA_DIR / BACKUP_SUBDIR / _utc_session_id()
        session_files = session_dir / "files"
        session_dir.mkdir(parents=True, exist_ok=True)

    all_applied_ids: list[str] = []

    try:
        for p in planned:
            for path in p.paths_to_backup:
                if not _path_allowed(path):
                    raise RuntimeError(f"refusing to touch disallowed path: {path}")
                if session_files is not None and path.is_file():
                    _backup_one_file(session_files, path)
                    backed_rels.append(
                        str(path.resolve().relative_to(DATA_DIR.resolve()))
                    )
            p.apply_fn()
            all_applied_ids.append(p.fix_id)
    except Exception as exc:
        echo_err(f"Stopped after error (no further operations): {exc}")
        if session_dir is not None:
            echo_err(f"Partial backup may be under: {session_dir}")
        return 1

    if session_dir is not None and session_files is not None:
        _write_meta(
            session_dir,
            list(argv),
            all_applied_ids,
            backed_rels,
            dry_run=dry_run,
            yes=yes,
            no_backup=no_backup,
        )
        echo(f"Backup session: {session_dir}")

    echo("Done.")
    return 0


# ---------------------------------------------------------------------------
# Section 3 CLI entry points
# ---------------------------------------------------------------------------

doctor_app = typer.Typer(
    name="doctor",
    help="Run diagnostic checks on your markbot installation.",
    no_args_is_help=False,
)


def _banner() -> None:
    console.print(f"\n{__logo__} MarkBot Doctor\n")


def _section(title: str, color: str = "cyan") -> None:
    W = 60
    title_text = f"  {title}  "
    pad = W - len(title_text) - 2
    line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'鈹€' * pad}[/]")
    console.print(line)


def _kv(key: str, value: str, key_w: int = 16) -> None:
    line = Text.from_markup(f"  [cyan]{key:<{key_w}}[/cyan] {value}")
    console.print(line)


def _ok(detail: str) -> None:
    console.print(Text.from_markup(f"  [green]OK[/green] {detail}"))


def _fail(detail: str) -> None:
    console.print(Text.from_markup(f"  [red]FAIL[/red] {detail}"))


def _note(detail: str) -> None:
    console.print(Text.from_markup(f"  [yellow]Note:[/yellow] {detail}"))


def _hint(message: str) -> None:
    console.print(Text.from_markup(f"  [dim]Hint: {message}[/dim]"))


def _divider() -> None:
    console.print(Text.from_markup(f"[dim]{'鈹€' * 58}[/dim]"))


@doctor_app.callback(invoke_without_command=True)
def doctor_main(
    ctx: typer.Context,
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Run extra checks: provider connectivity, channel reachability.",
    ),
) -> None:
    """Run diagnostic checks on your markbot installation."""
    if ctx.invoked_subcommand is not None:
        return

    _banner()

    failed = False

    # 鈹€鈹€ Environment 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Environment")
    for line in environment_summary_lines():
        console.print(f"  {line}")

    py_notes = check_python_version()
    for note in py_notes:
        _note(note)
        failed = True

    _divider()

    # 鈹€鈹€ Config 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Config")
    config_ok, config_detail = check_config_file()
    if config_ok:
        _ok(config_detail)
    else:
        _fail(config_detail)
        failed = True
        _hint("run 'markbot onboard' to create a valid config")

    unknown_keys = check_config_unknown_keys()
    if unknown_keys:
        for note in unknown_keys:
            _note(note)

    _divider()

    # 鈹€鈹€ Load config for further checks 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    cfg = None
    if config_ok:
        try:
            cfg = load_config()
        except Exception as e:
            _fail(f"cannot load config: {e}")
            failed = True

    if cfg is None:
        _section("Skipped", "yellow")
        _note("Config is invalid remaining checks skipped.")
        _hint("Fix config first, then re-run 'markbot doctor'.")
        console.print()
        if failed:
            raise typer.Exit(1)
        return

    # 鈹€鈹€ Model Chain 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Model Chain")
    chain_ok, chain_detail = check_model_chain(cfg)
    if chain_ok:
        _ok(chain_detail)
    else:
        _fail(chain_detail)
        failed = True
        _hint("configure model_chain in config.json, e.g. ['openai/gpt-4o']")

    primary_ok, primary_detail = check_primary_provider_configured(cfg)
    if primary_ok:
        _ok(f"primary provider: {primary_detail}")
    else:
        _fail(f"primary provider: {primary_detail}")
        failed = True

    prov_notes = check_all_providers_credentials(cfg)
    for note in prov_notes:
        _note(note)

    _divider()

    # 鈹€鈹€ Workspace 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Workspace")
    ws_ok, ws_detail = check_workspace(cfg)
    if ws_ok:
        _ok(ws_detail)
    else:
        _fail(ws_detail)
        failed = True

    ws_notes = check_workspace_subdirs(cfg)
    for note in ws_notes:
        _note(note)

    disk_notes = check_workspace_disk_space(cfg)
    for note in disk_notes:
        _note(note)

    _divider()

    # 鈹€鈹€ Channels 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Channels")
    ch_ok, ch_detail = check_channels(cfg)
    if ch_ok:
        _ok(ch_detail)
    else:
        _fail(ch_detail)
        failed = True

    cred_notes = check_channel_credentials(cfg)
    for note in cred_notes:
        _note(note)

    _divider()

    # 鈹€鈹€ MCP Servers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("MCP Servers")
    mcp_notes = check_mcp_servers(cfg)
    if mcp_notes:
        for note in mcp_notes:
            _note(note)
    else:
        mcp_count = len(cfg.tools.mcp_servers)
        _ok(f"{mcp_count} server(s) configured")

    _divider()

    # 鈹€鈹€ Skills 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Skills")
    sk_ok, sk_detail = check_skills(cfg)
    if sk_ok:
        _ok(sk_detail)
    else:
        _fail(sk_detail)
        failed = True

    _divider()

    # 鈹€鈹€ Memory / Embedding 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Memory / Embedding")
    mem_notes = check_memory_config(cfg)
    if mem_notes:
        for note in mem_notes:
            _note(note)
    else:
        _ok("no memory/embedding warnings")

    _divider()

    # 鈹€鈹€ Cron 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Cron")
    cron_ok, cron_detail = check_cron_jobs(cfg)
    if cron_ok:
        _ok(cron_detail)
    else:
        _fail(cron_detail)
        failed = True

    _divider()

    # 鈹€鈹€ Sessions 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Sessions")
    sess_ok, sess_detail = check_sessions(cfg)
    if sess_ok:
        _ok(sess_detail)
    else:
        _fail(sess_detail)
        failed = True

    _divider()

    # 鈹€鈹€ Gateway 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Gateway")
    gw_ok, gw_detail = check_gateway_pid()
    if gw_ok:
        _ok(gw_detail)
    else:
        _note(gw_detail)

    log_ok, log_detail = check_gateway_log()
    if log_ok:
        _kv("Log", log_detail)
    else:
        _note(log_detail)

    _divider()

    # 鈹€鈹€ Data Directory 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Data Directory")
    dd_ok, dd_detail = check_data_dir_writable()
    if dd_ok:
        _ok(dd_detail)
    else:
        _fail(dd_detail)
        failed = True

    _divider()

    # 鈹€鈹€ Optional Dependencies 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _section("Optional Dependencies")
    dep_notes = check_optional_dependencies()
    if dep_notes:
        for note in dep_notes:
            _note(note)
    else:
        _ok("all optional dependencies installed")

    _divider()

    # 鈹€鈹€ Deep Checks 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    if deep:
        _section("Deep Checks", "magenta")

        if cfg.agents.defaults.model_chain:
            _check_provider_connectivity(cfg)

        _check_channel_connectivity(cfg)

        _divider()

    # 鈹€鈹€ Summary 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    console.print()
    if failed:
        console.print(
            Text.from_markup(
                "  [bold red]鉁?Issues found[/bold red] review the FAIL items above"
            )
        )
        raise typer.Exit(1)
    else:
        console.print(
            Text.from_markup(
                "  [bold green]鉁?All checks passed[/bold green]"
            )
        )


def _check_provider_connectivity(cfg: Config) -> None:
    import asyncio

    chain = cfg.agents.defaults.model_chain
    if not chain:
        return

    ref = chain[0]
    _kv("Active LLM", ref)

    try:
        provider_cfg, model_cfg = cfg.resolve_model(ref)
    except ValueError as e:
        _fail(f"cannot resolve model: {e}")
        return

    provider_name = ref.split("/", 1)[0]

    from markbot.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    is_local = spec.is_local if spec else False
    is_oauth = spec.is_oauth if spec else False

    if is_local:
        _note("local provider skipping connectivity check")
        return

    if is_oauth:
        _note("OAuth provider skipping connectivity check")
        return

    if not provider_cfg.api_key:
        env_key = spec.env_key if spec else ""
        if env_key:
            env_val = os.getenv(env_key, "")
            if not env_val:
                _fail("no API key available for connectivity check")
                return
        else:
            _fail("no API key available for connectivity check")
            return

    try:
        from markbot.providers.fallback import FallbackManager

        manager = FallbackManager(cfg)

        async def _ping() -> tuple[bool, str]:
            try:
                provider = manager._get_or_create_provider(provider_cfg, provider_name)

                messages = [{"role": "user", "content": "Hi"}]
                response = await provider.chat(
                    messages=messages,
                    max_tokens=5,
                    temperature=0.0,
                )
                if response and response.content:
                    return True, "reachable (got response)"
                return True, "reachable (empty response)"
            except Exception as e:
                return False, str(e)[:200]

        ok, detail = asyncio.run(_ping())
        if ok:
            _ok(f"connectivity: {detail}")
        else:
            _fail(f"connectivity: {detail}")

    except Exception as e:
        _note(f"connectivity check skipped: {e}")


def _check_channel_connectivity(cfg: Config) -> None:
    import socket

    from markbot.channels.discovery import discover_all

    channel_classes = discover_all()
    checked = 0

    for name in cfg.channels.model_fields:
        section = getattr(cfg.channels, name, None)
        if section is None:
            continue

        if isinstance(section, dict):
            is_enabled = section.get("enabled", False)
        elif hasattr(section, "enabled"):
            is_enabled = getattr(section, "enabled", False)
        else:
            is_enabled = False

        if not is_enabled:
            continue

        if name not in channel_classes:
            continue

        host = _extract_channel_host(name, section)
        if not host:
            continue

        checked += 1
        port = _extract_channel_port(name, section)
        if port:
            try:
                with socket.create_connection((host, port), timeout=5):
                    _ok(f"{name}: {host}:{port} reachable")
            except OSError as e:
                _note(f"{name}: {host}:{port} {e}")
        else:
            _note(f"{name}: no host/port to probe")

    if checked == 0:
        _note("no enabled channels with network endpoints to probe")


def _extract_channel_host(name: str, section: Any) -> str | None:
    if isinstance(section, dict):
        for key in ("host", "ws_host", "server", "base_url", "url"):
            val = section.get(key, "")
            if val and isinstance(val, str):
                from urllib.parse import urlparse

                parsed = urlparse(val if "://" in val else f"scheme://{val}")
                return parsed.hostname or val.split(":")[0].strip()
    else:
        for attr in ("host", "ws_host", "server"):
            val = getattr(section, attr, None)
            if val and isinstance(val, str):
                return val.strip()
    return None


def _extract_channel_port(name: str, section: Any) -> int | None:
    if isinstance(section, dict):
        for key in ("port", "ws_port"):
            val = section.get(key)
            if val:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
    else:
        for attr in ("port", "ws_port"):
            val = getattr(section, attr, None)
            if val:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
    return None


# 鈹€鈹€ fix sub-command 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@doctor_app.command("fix")
def doctor_fix(
    only: str = typer.Option(
        None,
        "--only",
        help="Comma-separated fix ids to run. Default: safe fixes only.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview planned operations without writing files.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Apply risky fixes without confirmation.",
    ),
    no_backup: bool = typer.Option(
        False,
        "--no-backup",
        help="Skip backup before modifying files.",
    ),
    list_fixes: bool = typer.Option(
        False,
        "--list",
        help="List available fix ids and exit.",
    ),
) -> None:
    """Apply automated fixes for common issues."""
    if list_fixes:
        console.print("\nAvailable fix ids:\n")
        console.print("  [green]Safe[/green] (run by default, no --yes needed):")
        for fid in sorted(SAFE_FIX_IDS):
            console.print(f"    {fid}")
        console.print()
        console.print("  [yellow]Risky[/yellow] (require --yes / -y):")
        for fid in sorted(RISKY_FIX_IDS):
            console.print(f"    {fid}")
        console.print()
        return

    _banner()

    def _echo(msg: str) -> None:
        console.print(f"  {msg}")

    def _echo_err(msg: str) -> None:
        console.print(Text.from_markup(f"  [red]{msg}[/red]"))

    def _confirm(msg: str) -> bool:
        return typer.confirm(f"  {msg}", default=False)

    rc = run_doctor_fix(
        dry_run=dry_run,
        yes=yes,
        only=only,
        no_backup=no_backup,
        echo=_echo,
        echo_err=_echo_err,
        confirm_fn=_confirm,
        argv=sys.argv,
    )
    raise typer.Exit(rc)

