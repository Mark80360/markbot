from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.responses import JSONResponse

router = APIRouter()

SECRET_PATTERNS = {"API_KEY", "KEY", "PASSWORD", "SECRET", "TOKEN", "CREDENTIAL"}


def _is_secret(key: str) -> bool:
    upper = key.upper()
    return any(p in upper for p in SECRET_PATTERNS)


def _get_env_file() -> Path:
    """Get the .env file path. Checks workspace and home directory."""
    from markbot.config.loader import load_config
    try:
        cfg = load_config()
        workspace = cfg.workspace_path
        env_path = workspace / ".env"
        if env_path.exists():
            return env_path
    except Exception:
        pass
    # Default to home .markbot/.env
    return Path.home() / ".markbot" / ".env"


def _list_env() -> list[dict[str, Any]]:
    """List environment variables, merging .env file and process env.

    Variables are marked with `source`: "file" (from .env file),
    "process" (only in process env), or "both".
    """
    # Get variables from .env file
    env_path = _get_env_file()
    file_vars = _parse_env_file(env_path)

    results = []
    seen_keys = set()

    # First, add all file variables (merged with process value if also in process)
    for k in sorted(file_vars.keys()):
        is_secret = _is_secret(k)
        process_val = os.environ.get(k)
        file_val = file_vars[k]
        # Process env takes precedence for the actual value
        actual_val = process_val if process_val is not None else file_val
        source = "both" if process_val is not None else "file"
        results.append({
            "key": k,
            "value": "****" if is_secret and actual_val else actual_val,
            "is_secret": is_secret,
            "source": source,
        })
        seen_keys.add(k)

    # Then, add process-only variables (not in .env file)
    # Filter out common system variables that aren't useful to show
    SYSTEM_VAR_PREFIXES = ("_", "PS1", "PS2", "SHLVL", "PWD", "OLDPWD", "TERM", "SHLVL")
    for k in sorted(os.environ.keys()):
        if k in seen_keys:
            continue
        if k.startswith(SYSTEM_VAR_PREFIXES):
            continue
        is_secret = _is_secret(k)
        v = os.environ[k]
        results.append({
            "key": k,
            "value": "****" if is_secret and v else v,
            "is_secret": is_secret,
            "source": "process",
        })

    return results


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict."""
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Remove surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def _write_env_file(path: Path, env_dict: dict[str, str]) -> None:
    """Write a dict to a .env file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k in sorted(env_dict.keys()):
        v = env_dict[k]
        # Quote values that contain spaces or special chars
        if " " in v or "#" in v or v.startswith(("'", '"')):
            lines.append(f'{k}="{v}"')
        else:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class EnvSet(BaseModel):
    key: str
    value: str


class EnvReveal(BaseModel):
    key: str


class EnvImport(BaseModel):
    content: str  # .env file content


@router.get("/api/env")
async def list_env():
    return JSONResponse({"env": _list_env()})


@router.put("/api/env")
async def set_env(data: EnvSet):
    # Set in current process
    os.environ[data.key] = data.value

    # Persist to .env file
    try:
        env_path = _get_env_file()
        env_dict = _parse_env_file(env_path)
        env_dict[data.key] = data.value
        _write_env_file(env_path, env_dict)
    except Exception:
        pass  # Still return ok since we set it in the process env

    return JSONResponse({"ok": True})


@router.delete("/api/env")
async def delete_env(key: str):
    os.environ.pop(key, None)

    # Remove from .env file
    try:
        env_path = _get_env_file()
        env_dict = _parse_env_file(env_path)
        if key in env_dict:
            del env_dict[key]
            _write_env_file(env_path, env_dict)
    except Exception:
        pass

    return JSONResponse({"ok": True})


@router.post("/api/env/reveal")
async def reveal_env(data: EnvReveal):
    value = os.environ.get(data.key, "")
    return JSONResponse({"key": data.key, "value": value})


@router.get("/api/env/export")
async def export_env():
    """Export all environment variables as a .env file."""
    env_path = _get_env_file()
    env_dict = _parse_env_file(env_path)
    # Merge with current process env (process env takes precedence)
    for k, v in os.environ.items():
        if not _is_secret(k) or k not in env_dict:
            env_dict[k] = v

    lines = []
    for k in sorted(env_dict.keys()):
        v = env_dict[k]
        if _is_secret(k):
            v = "****" if v else ""
        if " " in v or "#" in v:
            lines.append(f'{k}="{v}"')
        else:
            lines.append(f"{k}={v}")

    from starlette.responses import Response
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename=".env"'},
    )


@router.post("/api/env/import")
async def import_env(data: EnvImport):
    """Import environment variables from .env file content."""
    import tempfile
    try:
        # Parse the content
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(data.content)
            temp_path = Path(f.name)

        env_dict = _parse_env_file(temp_path)
        temp_path.unlink()

        # Set in process and persist
        env_path = _get_env_file()
        existing = _parse_env_file(env_path)
        for k, v in env_dict.items():
            os.environ[k] = v
            existing[k] = v
        _write_env_file(env_path, existing)

        return JSONResponse({"ok": True, "imported": len(env_dict)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/env/file")
async def get_env_file():
    """Get the .env file path and its raw content."""
    env_path = _get_env_file()
    content = ""
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
    return JSONResponse({"path": str(env_path), "content": content, "exists": env_path.exists()})
