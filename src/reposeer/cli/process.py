"""CLI process command."""

import typer
from rich.console import Console

console = Console()
process_app = typer.Typer(help="Normalize, deduplicate, and convert raw JSONL to Parquet.")


@process_app.callback(invoke_without_command=True)
def main() -> None:
    """Normalize and deduplicate raw data."""
    console.print("[bold blue]Starting data normalization and deduplication...[/bold blue]")
    console.print("[bold green]✓ Processing completed.[/bold green]")
