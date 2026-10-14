"""CLI collect command."""

from pathlib import Path

import typer
from rich.console import Console

from reposeer.exceptions import CollectionError
from reposeer.services.repository_service import RepositoryService, parse_repository_target

console = Console()
collect_app = typer.Typer(
    help="Collect current GitHub repository metadata (github only).",
    context_settings={"allow_interspersed_args": True},
)


def _collect_batch(repos_file: Path) -> None:
    try:
        targets = [
            line.strip()
            for line in repos_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeError) as error:
        console.print("Cannot read repositories file as UTF-8.", style="bold red")
        raise typer.Exit(code=1) from error
    if not 1 <= len(targets) <= 1000:
        console.print("Repositories file must contain 1 to 1,000 targets.", style="bold red")
        raise typer.Exit(code=1)
    service = RepositoryService()
    try:
        result = service.collect_many(targets)
    except (CollectionError, ValueError) as error:
        console.print(
            "GitHub repository batch failed during processing or persistence.", style="bold red"
        )
        raise typer.Exit(code=1) from error
    finally:
        service.close()
    console.print("GitHub repository metadata batch", style="bold")
    for label, value in (
        ("Requested", result.requested_count),
        ("Raw stored", result.raw_stored_count),
        ("Processed stored", result.processed_stored_count),
        ("Excluded", result.excluded_count),
        ("Duplicates", result.duplicate_count),
        ("Failed", result.failed_count),
        ("Status", result.status),
    ):
        console.print(f"{label}: {value}", markup=False)
    for item in result.items:
        if item.status in {"failed", "excluded"}:
            reason = item.error_code or ", ".join(item.screening_reasons)
            console.print(f"{item.target}: {reason}", markup=False)
    if result.status != "complete":
        raise typer.Exit(code=1)


@collect_app.callback(invoke_without_command=True)
def main(
    target: str | None = typer.Argument(
        None, help="Repository target in format 'owner/repo' or GitHub URL"
    ),
    repos_file: Path | None = typer.Option(
        None,
        "--repos-file",
        help="UTF-8 text file containing one GitHub repository target per line",
    ),
    source: str = typer.Option(
        "github", "--source", "-s", help="Supported source: github; pypi/external/all are not implemented"
    ),
) -> None:
    """Collect one repository or a batch of current GitHub repository metadata."""
    if source != "github":
        console.print(f"Unsupported source '{source}': only github is implemented.", style="bold red")
        raise typer.Exit(code=2)
    if (target is None) == (repos_file is None):
        console.print("Provide exactly one target or --repos-file.", style="bold red")
        raise typer.Exit(code=1)
    if repos_file is not None:
        _collect_batch(repos_file)
        return
    try:
        target = parse_repository_target(target)
    except ValueError as error:
        console.print("Expected owner/repo or HTTPS GitHub repository URL.", style="bold red")
        raise typer.Exit(code=1) from error
    console.print(
        f"Starting collection for: {target} (source: {source})", style="bold green", markup=False
    )
    from reposeer.pipelines.full import FullPipeline

    pipeline = FullPipeline()
    try:
        pipeline.run(target)
        console.print(f"[bold green]✓ Collection completed successfully for {target}[/bold green]")
    except (CollectionError, ValueError, OSError) as error:
        console.print("Collection failed during processing or persistence.", style="bold red")
        raise typer.Exit(code=1) from error
