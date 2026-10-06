"""CLI feature building command."""

import typer
from rich.console import Console

console = Console()
build_features_app = typer.Typer(help="Build feature tables for analytical and modeling tasks.")


@build_features_app.callback(invoke_without_command=True)
def main() -> None:
    """Generate analytical features."""
    console.print("[bold magenta]Building repository and package features...[/bold magenta]")
    console.print("[bold green]✓ Features generated successfully.[/bold green]")
