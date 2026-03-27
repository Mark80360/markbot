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

from markbot.agent.skills import SkillsLoader
from markbot.agent.tools.registry import ToolRegistry
from markbot.config.paths import get_workspace_path

app = typer.Typer(
    name="skills",
    help="Manage and execute skill scripts",
    no_args_is_help=True,
)

console = Console()


def _get_loader() -> SkillsLoader:
    """Get configured SkillsLoader instance."""
    workspace = get_workspace_path()
    # We don't have a tool registry for CLI operations
    return SkillsLoader(workspace=workspace, tool_registry=None)


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
    loader = _get_loader()
    skills = loader.list_skills()

    if not skills:
        console.print("[yellow]No skills found.[/yellow]")
        return

    table = Table(
        title="Available Skills",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Executable", style="yellow")
    table.add_column("Description", style="white")

    for skill in skills:
        if executable_only and skill.get("executable") != "true":
            continue

        meta = loader.get_skill_metadata(skill["name"])
        desc = ""
        if meta:
            desc = meta.get("description", "")[:60]
            if len(desc) == 60:
                desc += "..."

        table.add_row(
            skill["name"],
            skill["source"],
            skill.get("executable", "false"),
            desc,
        )

    console.print(table)

    if show_scripts:
        scripts = loader.list_executable_scripts()
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
    loader = _get_loader()

    console.print(f"[cyan]Scanning {skill}.{script}...[/cyan]")

    try:
        result = loader.scan_script(skill, script)

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


@app.command("validate")
def validate_skill(
    skill_name: str = typer.Argument(..., help="Skill name to validate"),
):
    """Validate a skill's metadata and scripts."""
    loader = _get_loader()

    console.print(f"[cyan]Validating skill: {skill_name}...[/cyan]")

    try:
        errors = loader.validate_skill(skill_name)

        if errors:
            console.print("[red]✗ Validation failed:[/red]")
            for error in errors:
                console.print(f"  • {error}")
            raise typer.Exit(1)
        else:
            console.print("[green]✓ Skill is valid[/green]")

            # Show additional info for executable skills
            meta = loader.get_skill_metadata(skill_name)
            if meta:
                metadata_value = meta.get("metadata", {})
                try:
                    # Handle both dict (nested YAML) and string (JSON) formats
                    if isinstance(metadata_value, str):
                        markbot_meta = json.loads(metadata_value)
                    else:
                        markbot_meta = metadata_value
                    
                    if markbot_meta.get("markbot", {}).get("executable"):
                        scripts = loader.list_executable_scripts(skill_name)
                        console.print(f"\nFound {len(scripts)} executable script(s):")
                        for script in scripts:
                            console.print(f"  • {script['name']} ({script['language']})")
                except (json.JSONDecodeError, KeyError, AttributeError):
                    pass

    except Exception as e:
        console.print(f"[red]Error validating skill: {e}[/red]")
        raise typer.Exit(1)


@app.command("run")
def run_script(
    full_name: str = typer.Argument(
        ...,
        help="Script identifier (format: skill.script)",
    ),
    args: Optional[str] = typer.Option(
        None,
        "--args",
        "-a",
        help='Script arguments as JSON (e.g., \'{"key": "value"}\')',
    ),
    timeout: int = typer.Option(
        30,
        "--timeout",
        "-t",
        help="Script execution timeout in seconds",
    ),
):
    """Execute a skill script with arguments."""
    
    # Parse skill.script format
    if "." not in full_name:
        console.print("[red]Error: Please use format 'skill.script'[/red]")
        raise typer.Exit(1)

    skill_name, script_name = full_name.split(".", 1)

    # Parse arguments
    script_args = {}
    if args:
        try:
            script_args = json.loads(args)
        except json.JSONDecodeError as e:
            console.print(f"[red]Error parsing JSON arguments: {e}[/red]")
            raise typer.Exit(1)

    loader = _get_loader()

    console.print(f"[cyan]Running {full_name}...[/cyan]")
    if script_args:
        console.print(f"[dim]Arguments: {script_args}[/dim]")

    try:
        # Override timeout in sandbox config if provided
        scripts = loader.list_executable_scripts(skill_name)
        script_info = next((s for s in scripts if s["name"] == script_name), None)
        
        if script_info and "sandbox" in script_info:
            script_info["sandbox"]["timeout"] = timeout

        result = loader.execute_script(skill_name, script_name, script_args)

        if result.startswith("Error:"):
            console.print(f"[red]{result}[/red]")
            raise typer.Exit(1)
        else:
            console.print(result)

    except Exception as e:
        console.print(f"[red]Error running script: {e}[/red]")
        raise typer.Exit(1)


@app.command("info")
def skill_info(
    skill_name: str = typer.Argument(..., help="Skill name"),
):
    """Show detailed information about a skill."""
    loader = _get_loader()

    meta = loader.get_skill_metadata(skill_name)
    if not meta:
        console.print(f"[red]Skill '{skill_name}' not found[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]Skill: {skill_name}[/bold cyan]\n")

    if meta.get("description"):
        console.print(f"[bold]Description:[/bold] {meta['description']}\n")

    # Check if executable
    metadata_value = meta.get("metadata", {})
    try:
        # Handle both dict and string formats
        if isinstance(metadata_value, str):
            markbot_meta = json.loads(metadata_value).get("markbot", {})
        else:
            markbot_meta = metadata_value.get("markbot", {})
        
        if markbot_meta.get("executable"):
            console.print("[green]✓ Executable skill[/green]\n")

            scripts = loader.list_executable_scripts(skill_name)
            if scripts:
                console.print("[bold]Scripts:[/bold]")
                for script in scripts:
                    console.print(f"\n  [green]{script['name']}[/green]")
                    console.print(f"    Description: {script.get('description', 'N/A')}")
                    console.print(f"    Language: {script['language']}")
                    console.print(f"    Entry: {script['entry']}")
                    
                    if "parameters" in script:
                        console.print(f"    Parameters:")
                        params = script["parameters"]
                        required = params.get("required", [])
                        for prop_name, prop_info in params.get("properties", {}).items():
                            req_mark = "*" if prop_name in required else ""
                            console.print(f"      - {prop_name}{req_mark}: {prop_info.get('type', 'any')}")
                    
                    if "sandbox" in script:
                        console.print(f"    Sandbox config:")
                        for key, value in script["sandbox"].items():
                            console.print(f"      - {key}: {value}")
        else:
            console.print("[dim]Not an executable skill (no scripts)[/dim]\n")
            
    except (json.JSONDecodeError, KeyError):
        console.print("[dim]No executable metadata found[/dim]\n")

    # Show content preview
    content = loader.load_skill(skill_name)
    if content:
        preview = loader._strip_frontmatter(content)[:500]
        if len(preview) == 500:
            preview += "..."
        console.print("[bold]Content Preview:[/bold]")
        console.print(preview)
