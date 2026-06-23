"""``markbot provider`` group: LLM provider OAuth login."""
from __future__ import annotations

import typer

from markbot.cli.ui import console, make_section_helpers, markbot_banner

app = typer.Typer(help="Manage providers")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@app.command("login")
def login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from markbot.providers.registry import PROVIDERS

    markbot_banner()

    section, kv, divider = make_section_helpers()

    console.print()

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        section("Error", "red")
        console.print(f"  [red]✗[/red] Unknown OAuth provider: [cyan]{provider}[/cyan]")
        kv("Supported", ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth))
        divider()
        console.print()
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        section("Error", "red")
        console.print(f"  [red]✗[/red] Login not implemented for [cyan]{spec.label}[/cyan]")
        divider()
        console.print()
        raise typer.Exit(1)

    # ─ OAuth Login ───────────────────────────────────────────────────────────
    section(f"{spec.label} Login", "cyan")
    kv("Provider", spec.name.replace("_", "-"))
    kv("Type", "OAuth")
    divider()
    console.print()

    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    """Authenticate with GitHub Copilot via the device-flow endpoints.

    Uses Copilot's documented device-code endpoints directly rather than
    relying on a fake API key to trigger a 401, which is brittle and
    produces misleading error messages when the underlying library
    changes its validation behaviour.
    """
    import json
    import time
    import urllib.request
    import webbrowser

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    client_id = "Iv1.b507a5c18083c458"  # public Copilot CLI client id
    device_url = "https://github.com/login/device/code"
    token_url = "https://github.com/login/oauth/access_token"
    api_root = "https://api.githubcopilot.com"

    # 1. Request device code
    req = urllib.request.Request(
        device_url,
        data=json.dumps({"client_id": client_id, "scope": "read:user"}).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        console.print(f"[red]✗ Failed to request device code: {e}[/red]")
        raise typer.Exit(1)

    user_code = payload.get("user_code", "")
    verification_uri = payload.get("verification_uri", "https://github.com/login/device")
    interval = int(payload.get("interval", 5))
    expires_in = int(payload.get("expires_in", 900))
    device_code = payload.get("device_code", "")

    console.print(f"  [bold]User code:[/bold] {user_code}")
    console.print(f"  Open: [cyan]{verification_uri}[/cyan]\n")
    try:
        webbrowser.open(verification_uri)
    except Exception:
        pass  # headless environment

    # 2. Poll for token
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        token_req = urllib.request.Request(
            token_url,
            data=json.dumps({
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(token_req, timeout=30) as resp:
                tdata = json.loads(resp.read().decode())
        except Exception as e:
            console.print(f"[yellow]polling error: {e}[/yellow]")
            continue

        if "access_token" in tdata:
            access_token = tdata["access_token"]
            # 3. Exchange GitHub token for Copilot session token
            exchange_req = urllib.request.Request(
                f"{api_root}/copilot_internal/v2/token",
                headers={
                    "Authorization": f"token {access_token}",
                    "Editor-Version": "vscode/1.85.0",
                    "Editor-Plugin-Version": "copilot-chat/0.0.1",
                    "Accept": "application/json",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(exchange_req, timeout=30) as resp:
                    cdata = json.loads(resp.read().decode())
                copilot_token = cdata.get("token")
                if copilot_token:
                    console.print(
                        f"[green]✓ Authenticated with GitHub Copilot[/green] "
                        f"[dim]expires in {cdata.get('expires_at', '?')}[/dim]"
                    )
                    return
            except Exception as e:
                console.print(f"[red]✗ Copilot token exchange failed: {e}[/red]")
                raise typer.Exit(1)
        elif tdata.get("error") == "authorization_pending":
            continue
        elif tdata.get("error") == "slow_down":
            interval += 5
            continue
        elif tdata.get("error") == "expired_token":
            console.print("[red]✗ Device code expired. Please try again.[/red]")
            raise typer.Exit(1)
        else:
            console.print(f"[red]✗ Unexpected response: {tdata}[/red]")
            raise typer.Exit(1)

    console.print("[red]✗ Timed out waiting for authentication.[/red]")
    raise typer.Exit(1)
