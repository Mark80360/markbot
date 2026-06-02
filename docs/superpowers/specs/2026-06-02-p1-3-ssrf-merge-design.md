# P1-3: SSRF Module Consolidation Design

> **Goal:** Merge `utils/network.py` + `utils/url_safety.py` into a single config-driven SSRF protection module.

## Problem

Two files with 80%+ overlap, different API styles, and gaps in security coverage:

| File | Lines | Public API | Style | Tests |
|------|-------|--------|-------|-------|
| `utils/network.py` | 131 | `validate_url_target` / `validate_resolved_url` / `contains_internal_url` | `(bool, str)` | 186 lines |
| `utils/url_safety.py` | 102 | `check_url_safety(url, allow_private=False)` | `str \| None` | 0 |

**Security gaps found:**
1. `network.py` lacks cloud metadata hostname blocking (`metadata.google.internal`)
2. `url_safety.py` lacks `validate_resolved_url` (post-redirect SSRF verification)
3. `network.py` misses `198.18.0.0/15` (benchmarking), `url_safety.py` misses `0.0.0.0/8`
4. `url_safety.py` has `100.100.100.200` (Alibaba cloud metadata) — `network.py` doesn't
5. `browser.py` uses `url_safety` only for pre-fetch validation, no redirect target protection
6. Block lists are hardcoded — not configurable per deployment

## Architecture

Single file `markbot/utils/ssrf.py`. Block lists configurable via `config.json`.

```
User config (json)
    │
    ▼
Config.schema.SsrfConfig  ──→  init_from_config()
    │                                │
    │                                ▼
    │                     ssrf.py module state
    │                     (_PRIVATE_NETWORKS, _BLOCKED_HOSTNAMES, etc.)
    │                                │
    ▼                                ▼
3 public functions:
  validate_url_target(url, allow_private) -> (bool, str)
  validate_resolved_url(url, allow_private) -> (bool, str)
  contains_internal_url(command, allowed_ips, allow_private) -> bool
```

- No hardcoded block lists in `ssrf.py` — values come from Config
- Minimal safe fallback (loopback only) if `init_from_config` never called
- All 3 functions are pure reads against module state after init

## Public API

```python
def validate_url_target(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """Validate URL before fetch. Returns (ok, error_message)."""
    # Full validation: scheme → hostname → DNS → private IP / blocked hostname / always-blocked
```

```python
def validate_resolved_url(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """Validate already-fetched URL (e.g. after redirect). Skips DNS for IP URLs."""
    # Lightweight: parse hostname → check if private IP / always-blocked
    # If hostname is domain, resolve + check
```

```python
def contains_internal_url(
    command: str,
    allowed_ips: list[str] | None = None,
    allow_private: bool = False,
) -> bool:
    """Scan command string for URLs targeting private/internal addresses."""
    # Extract URLs via regex → validate each → respect allowed_ips whitelist
```

Return convention:
- `(True, "")` — safe
- `(False, "Blocked: ...")` — blocked by SSRF rule
- `(False, "Only http/https allowed...")` — format/parse error

## Block List Schema

New config section in `markbot/config/schema.py`:

```python
class SsrfConfig(Base):
    blocked_hostnames: list[str] = Field(
        default_factory=lambda: [
            "metadata.google.internal",
            "metadata.goog",
        ],
    )
    always_blocked_ips: list[str] = Field(
        default_factory=lambda: [
            "169.254.169.254", "169.254.170.2", "169.254.169.253",
            "fd00:ec2::254", "100.100.100.200",
        ],
    )
    blocked_networks: list[str] = Field(
        default_factory=lambda: [
            "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10",
            "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
            "192.168.0.0/16", "198.18.0.0/15",
            "::1/128", "fc00::/7", "fe80::/10",
        ],
    )
```

Added to root `Config`:
```python
class Config(BaseSettings):
    ssrf: SsrfConfig = Field(default_factory=SsrfConfig)
```

**Block logic priority:**
1. `_BLOCKED_HOSTNAMES` — if hostname matches, blocked
2. `_ALWAYS_BLOCKED_IPS` — if resolved IP matches, blocked (regardless of `allow_private`)
3. `_PRIVATE_NETWORKS` — if resolved IP is in these networks, blocked (unless `allow_private=True`)

## Module Init

```python
# ssrf.py — module-level state
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset()
_ALWAYS_BLOCKED_IPS: tuple[ipaddress._BaseAddress, ...] = ()
_PRIVATE_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = ()

def init_from_config(config: Config) -> None:
    """Populate SSRF block lists from Config.ssrf section."""
    # Parse and store in module-level state
```

Called early in startup flow (e.g. in `runtime.load_runtime_config`).

## Security Improvements

**browser.py redirect protection** — add post-redirect validation:

```python
from markbot.utils.ssrf import validate_url_target, validate_resolved_url

# Pre-fetch: block unsafe targets
ok, err = validate_url_target(url)
if not ok:
    return f"Error: {err}"

response = await page.goto(url, ...)
final_url = response.url if response else url

# Post-redirect: block redirect to private IP
if final_url != url:
    ok, err = validate_resolved_url(final_url)
    if not ok:
        return f"Error: redirect blocked - {err}"
```

## File Changes

| Action | File | Notes |
|--------|------|-------|
| **Create** | `markbot/utils/ssrf.py` | New merged module |
| **Modify** | `markbot/config/schema.py` | Add `SsrfConfig` + `Config.ssrf` |
| **Modify** | `markbot/config/loader.py` | Add `init_from_config` call in load flow |
| **Modify** | `markbot/tools/browser.py:163` | Migrate + add redirect check |
| **Modify** | `markbot/tools/web.py:90,357,434` | Migrate import |
| **Modify** | `markbot/tools/shell.py:224` | Migrate import |
| **Modify** | `markbot/channels/qq.py:40` | Migrate import |
| **Delete** | `markbot/utils/network.py` | Merged into ssrf.py |
| **Delete** | `markbot/utils/url_safety.py` | Merged into ssrf.py |
| **Create** | `tests/test_ssrf.py` | Migrate + extend from test_network.py |

## Migration Strategy

Zero behavior change for callers:
- All 3 public function signatures compatible with existing callers
- `validate_url_target(url)` → `validate_url_target(url)` — same call
- `contains_internal_url(command, allowed_ips)` → same
- Only `browser.py` gets behavioral improvement (redirect validation)
- `check_url_safety(url)` → `validate_url_target(url)` — change from `str | None` to `(bool, str)`

## Testing

- `tests/test_ssrf.py` (~220+ lines): migrate from `test_network.py` (186 lines) + add:
  - Cloud metadata hostname blocking
  - Always-blocked IPs
  - `allow_private=True` bypass
  - `contains_internal_url` with `allow_private`
  - `init_from_config` with various Config inputs
  - Post-redirect validation scenario
  - Config-driven: different block lists per test
- Direct `_is_private` / `_BLOCKED_NETWORKS` tests removed → implicit through public functions
- All tests call `init_from_config(Config())` (constructs default Config, no hardcoded values in ssrf.py)

## Future Considerations

- If more SSRF features needed (e.g. DNS rebinding detection, IPv6 transition mechanisms), add to this module
- `allow_private` at function level, but could add global default in `SsrfConfig` later if needed
- Block list reload (SIGHUP or config watch) possible but not in scope
