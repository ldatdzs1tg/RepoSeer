"""CLI feature building command."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from reposeer.pipelines.features import FeaturesPipeline
from reposeer.storage.paths import StoragePaths

console = Console()
build_features_app = typer.Typer(help="Build feature tables for analytical and modeling tasks.")


@build_features_app.callback(invoke_without_command=True)
def main(
    data_dir: Path = typer.Option(
        Path("data"), "--data-dir", help="Storage base directory."
    ),
    request_file: Path | None = typer.Option(
        None, "--request-file", help="Path to SnapshotBuildRequest JSON/JSONL file."
    ),
    package_id: str | None = typer.Option(
        None, "--package-id", help="Target package ID, e.g. pypi:flask."
    ),
    repo_id: str | None = typer.Option(
        None, "--repo-id", help="Target repository ID, e.g. github:repository:900000001."
    ),
    cutoff_at: list[str] | None = typer.Option(
        None, "--cutoff-at", help="One or more UTC cutoff timestamps."
    ),
    output_path: Path | None = typer.Option(
        None, "--output-path", help="Optional override for output Parquet path."
    ),
) -> None:
    """Generate analytical features."""
    paths = StoragePaths(base_dir=data_dir)
    pipeline = FeaturesPipeline(paths=paths)

    targets: list[tuple[str, str, str]] | None = None
    cutoffs: list[str] | None = cutoff_at

    if package_id is not None and repo_id is not None:
        if cutoff_at:
            targets = [(package_id, repo_id, c) for c in cutoff_at]
            cutoffs = None
        else:
            targets = None

    try:
        summary = pipeline.run(
            request_file=request_file,
            targets=targets,
            cutoffs=cutoffs,
            output_path=output_path,
            persist=True,
        )
    except Exception as exc:
        console.print(f"[bold red]Feature generation failed: {exc}[/bold red]")
        raise typer.Exit(code=2) from exc

    results = summary.results
    complete = sum(1 for r in results if r.status == "complete")
    partial = sum(1 for r in results if r.status == "partial")
    failed = sum(1 for r in results if r.status == "failed")
    total = len(results)

    console.print(f"Snapshots built: {total} total ({complete} complete, {partial} partial, {failed} failed)")

    if output_path is not None:
        console.print(f"Features saved to: {output_path}")
    else:
        console.print(f"Features saved to: {paths.features_path('repository_package_snapshots')}")

    if total == 0 or (total > 0 and failed == total):
        raise typer.Exit(code=1)
