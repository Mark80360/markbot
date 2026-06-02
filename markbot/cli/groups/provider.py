"""``markbot provider`` group: LLM provider OAuth login."""
from __future__ import annotations

import typer

from markbot.cli.ui import console, markbot_banner

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
    from rich.text import Text

    from markbot.providers.registry import PROVIDERS

    markbot_banner()

    W = 72  # total width

    def section(title: str, color: str = "cyan") -> None:
        title_text = f"  {title}  "
        pad = W - len(title_text) - 2
        line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
        console.print(line)

    def kv(key: str, value: str, key_w: int = 14) -> None:
        line = Text.from_markup(f"  [cyan]{key:<{key_w}}[/cyan] {value}")
        console.print(line)

    def divider() -> None:
        line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
        console.print(line)

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
    import asyncio

    from openai import AsyncOpenAI

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        client = AsyncOpenAI(
            api_key="dummy",
            base_url="https://api.githubcopilot.com",
        )
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)
