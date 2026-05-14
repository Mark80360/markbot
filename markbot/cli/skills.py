"""CLI commands for skill management.

Provides commands to:
- List all available skills and scripts
- Scan scripts for security issues
- Validate skill metadata
- Execute scripts directly
"""

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from markbot.skills import SkillRegistry
from markbot.config.paths import get_workspace_path

app = typer.Typer(
    name="skills",
    help="Manage and execute skill scripts",
    no_args_is_help=True,
)

console = Console()


def _get_registry() -> SkillRegistry:
    """Get configured SkillRegistry instance."""
    workspace = get_workspace_path()
    registry = SkillRegistry(workspace, tool_registry=None)
    registry.load_all()
    return registry


@app.command("list")
def list_skills(
    executable_only: bool = typer.Option(
        False, "--executable", "-e", help="Show only executable skills"
    ),
    show_scripts: bool = typer.Option(
        False, "--scripts", "-s", help="Show executable scripts"
    ),
):
    """List all available skills."""
    registry = _get_registry()
    skills = registry.list_all()

    if not skills:
        console.print("[yellow]No skills found.[/yellow]")
        return

    table = Table(
        title="Available Skills",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Scripts", style="yellow")

    for skill in skills:
        # Check if has executable scripts
        has_scripts = len(skill.scripts) > 0
        if executable_only and not has_scripts:
            continue

        desc = skill.description[:60] if len(skill.description) > 60 else skill.description
        if len(skill.description) > 60:
            desc += "..."

        table.add_row(
            skill.name,
            desc,
            str(len(skill.scripts)),
        )

    console.print(table)

    if show_scripts:
        scripts = registry.list_executable_scripts()
        if scripts:
            console.print("\n[bold]Executable Scripts:[/bold]")
            script_table = Table(show_header=True, header_style="bold magenta")
            script_table.add_column("Skill", style="cyan")
            script_table.add_column("Script", style="green")
            script_table.add_column("Description", style="white")

            for script in scripts:
                script_table.add_row(
                    script["skill_name"],
                    script["name"],
                    script.get("description", ""),
                )
            console.print(script_table)


@app.command("scan")
def scan_script(
    skill: str = typer.Argument(..., help="Skill name"),
    script: str = typer.Argument(..., help="Script name"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show all findings"),
):
    """Scan a script for security issues."""
    from loguru import logger

    logger.disable("markbot.core.skills.registry")
    registry = _get_registry()

    console.print(f"[cyan]Scanning {skill}.{script}...[/cyan]")

    try:
        result = registry.scan_script(skill, script)

        if result is None:
            console.print("[yellow]Security scanner not available[/yellow]")
            raise typer.Exit(1)

        if result.is_safe:
            console.print("[green]✓ No security issues found[/green]")
            console.print(f"Scan completed in {result.scan_duration_ms:.2f}ms")
        else:
            console.print("[red]✗ Security issues found:[/red]")

            # Group by severity
            high_findings = [f for f in result.findings if f.severity in ("high", "critical")]
            medium_findings = [f for f in result.findings if f.severity == "medium"]
            low_findings = [f for f in result.findings if f.severity == "low"]

            if high_findings:
                console.print("\n[bold red]High/Critical Issues:[/bold red]")
                for finding in high_findings:
                    console.print(f"  Line {finding.line}: [{finding.severity}] {finding.message}")

            if verbose:
                if medium_findings:
                    console.print("\n[bold yellow]Medium Issues:[/bold yellow]")
                    for finding in medium_findings:
                        console.print(f"  Line {finding.line}: {finding.message}")

                if low_findings:
                    console.print("\n[blue]Low Issues:[/blue]")
                    for finding in low_findings:
                        console.print(f"  Line {finding.line}: {finding.message}")
            else:
                remaining = len(medium_findings) + len(low_findings)
                if remaining > 0:
                    console.print(f"\n[{remaining} additional findings, use --verbose to see all]")

    except Exception as e:
        console.print(f"[red]Error scanning script: {e}[/red]")
        raise typer.Exit(1)
    finally:
        logger.enable("markbot.core.skills.registry")


@app.command("info")
def skill_info(
    skill_name: str = typer.Argument(..., help="Skill name"),
):
    """Show detailed information about a skill."""
    registry = _get_registry()

    skill = registry.get(skill_name)
    if not skill:
        console.print(f"[red]Skill '{skill_name}' not found[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]Skill: {skill_name}[/bold cyan]\n")
    console.print(f"[bold]Description:[/bold] {skill.description}\n")
    console.print(f"[bold]When to use:[/bold] {skill.when_to_use}\n")

    # Show scripts
    if skill.scripts:
        console.print("[green]✓ Executable skill[/green]\n")
        console.print(f"[bold]Scripts ({len(skill.scripts)}):[/bold]")
        for script in skill.scripts:
            console.print(f"\n  [green]{script.name}[/green]")
            console.print(f"    Description: {script.description}")
            console.print(f"    Language: {script.language}")
            console.print(f"    Entry: {script.entry}")
            if script.parameters:
                console.print(f"    Parameters:")
                for param in script.parameters:
                    req_mark = "*" if param.required else ""
                    console.print(f"      - {param.name}{req_mark}: {param.type}")
    else:
        console.print("[dim]No executable scripts[/dim]\n")

    # Show content preview
    content = registry.load_skill_content(skill_name)
    if content:
        preview = content[:500]
        if len(content) > 500:
            preview += "..."
        console.print("\n[bold]Content Preview:[/bold]")
        console.print(preview)
