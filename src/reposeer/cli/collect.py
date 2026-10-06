"""CLI collect command."""

import typer
from rich.console import Console

console = Console()
collect_app = typer.Typer(help="Collect repository metadata, activity, and external signals.")


@collect_app.callback(invoke_without_command=True)
def main(
    target: str = typer.Argument(
        ..., help="Repository target in format 'owner/repo' or GitHub URL"
    ),
    source: str = typer.Option(
        "all", "--source", "-s", help="Source to collect (github, pypi, external, all)"
    ),
) -> None:
    """Run collection on the specified repository."""
    console.print(f"[bold green]Starting collection for:[/bold green] {target} (source: {source})")
    from reposeer.pipelines.full import FullPipeline

    pipeline = FullPipeline()
    try:
        pipeline.run(target)
        console.print(f"[bold green]✓ Collection completed successfully for {target}[/bold green]")
    except Exception as e:
        console.print(f"[bold red]✗ Collection failed:[/bold red] {e}")
        raise typer.Exit(code=1) from e
