"""Offline T-018 normalization and T-019 processed dataset persistence."""

from pathlib import Path

import polars as pl
import typer
from pydantic import TypeAdapter
from rich.console import Console

from reposeer.exceptions import StorageError
from reposeer.pipelines.processed import ProcessedDatasetPipeline
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.storage.paths import StoragePaths

console = Console()
process_app = typer.Typer(help="Replay saved raw JSONL offline into canonical processed Parquet.")


@process_app.callback(invoke_without_command=True)
def main(
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Storage root; defaults to configured base directory."),
    raw: list[Path] | None = typer.Option(None, "--raw", help="Saved raw JSONL input; repeatable. Otherwise discover existing supported inputs."),
    targets_file: Path | None = typer.Option(None, "--targets-file", help="Strict JSON array of upstream VerifiedTargetContext objects."),
    cutoff_at: str | None = typer.Option(None, "--cutoff-at", help="Optional UTC YYYY-MM-DDTHH:mm:ss.sssZ for diagnostics, not historical proof."),
) -> None:
    """Normalize and persist local observations without APIs or LLM calls."""
    paths = StoragePaths(data_dir)
    inputs = raw if raw is not None else paths.processing_inputs()
    try:
        targets = (TypeAdapter(list[VerifiedTargetContext]).validate_json(targets_file.read_bytes(), strict=True)
                   if targets_file is not None else [])
        result = ProcessedDatasetPipeline(paths).run(inputs, targets=targets, cutoff_at=cutoff_at)
    except (OSError, ValueError, TypeError, StorageError, pl.exceptions.PolarsError) as error:
        console.print(f"[red]Processing failed ({type(error).__name__}); check processing manifests when available.[/red]")
        raise typer.Exit(code=2) from error
    console.print(f"Processing run ID: {result.processing_run_id}")
    console.print(f"Processing status: {result.status}")
    console.print(f"Raw inputs processed: {len(inputs)} files / {result.input_record_count} records")
    written = [name for name, info in result.datasets.items() if info.status == "written"]
    console.print("Processed datasets written: " + (", ".join(written) or "none (unchanged replay)"))
    for name, count in result.dataset_row_counts.items():
        console.print(f"Inserted rows {name}: {count}")
    console.print(f"Manifest path: {result.manifest_path}")
    console.print(f"Processing errors/rejections: {result.error_count}; source coverage: not_verified")
    if result.status != "complete":
        raise typer.Exit(code=1)
