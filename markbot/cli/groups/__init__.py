"""MarkBot CLI command groups.

Each module here owns one typer.Typer group (or single-command typer
app) that ``markbot.cli.commands`` wires into the top-level ``app``
via ``add_typer``. Groups are split by responsibility, not by command
count: ``agent`` owns the interactive chat loop and its prompt_toolkit
helpers; ``gateway`` owns the background gateway lifecycle; etc.
"""
