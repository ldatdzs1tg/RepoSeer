"""Generate evidenced PyPI target batches; collection is a separate command."""

import json
import sqlite3
from pathlib import Path

import typer

from reposeer.clients.pypi_index import DEFAULT_INDEX_BYTES, PackageDiscoveryError
from reposeer.pipelines.package_discovery import PackageDiscoveryOptions, PackageDiscoveryPipeline
from reposeer.storage.paths import StoragePaths

discover_packages_app = typer.Typer(
    help="Discover PyPI names and export reproducible target batches."
)


@discover_packages_app.callback(invoke_without_command=True)
def main(
    source: str = typer.Option(..., "--source", help="pypi-simple, snapshot or file"),
    count: int = typer.Option(..., "--count", help="Packages to select: 1-10000"),
    seed: int = typer.Option(42, "--seed", help="Integer SHA-256 sampling seed"),
    batch_size: int = typer.Option(50, "--batch-size", help="Targets per T-020 batch: 1-100"),
    data_dir: Path | None = typer.Option(
        None, "--data-dir", help="Storage root; defaults to configuration"
    ),
    snapshot: Path | None = typer.Option(
        None, "--snapshot", help="Verified .html snapshot with JSON sidecar"
    ),
    input_file: Path | None = typer.Option(
        None, "--input-file", help="UTF-8 PyPI names or pypi: IDs"
    ),
    exclude_file: Path | None = typer.Option(
        None, "--exclude-file", help="Explicit UTF-8 exclusions"
    ),
    max_index_bytes: int = typer.Option(
        DEFAULT_INDEX_BYTES, "--max-index-bytes", help="Decoded-byte budget, at most 512 MiB"
    ),
) -> None:
    options = PackageDiscoveryOptions(
        source=source,
        count=count,
        seed=seed,
        batch_size=batch_size,
        data_dir=StoragePaths(data_dir).base_dir,
        snapshot=snapshot,
        input_file=input_file,
        exclude_file=exclude_file,
        max_index_bytes=max_index_bytes,
    )
    try:
        options.validate()
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None
    try:
        result = PackageDiscoveryPipeline().run(options)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    except PackageDiscoveryError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    except (OSError, sqlite3.Error):
        typer.echo("package_discovery_storage_or_source_io_failed", err=True)
        raise typer.Exit(1) from None
    for label, value in (
        ("Sampling Run ID", result.sampling_run_id),
        ("Source", source),
        ("Index snapshot", result.snapshot_path or "not applicable"),
        ("Snapshot SHA256", manifest["source"]["snapshot_sha256"] or "not applicable"),
        ("Unique eligible names", manifest["counts"]["eligible_unique_candidates"]),
        ("Selected packages", result.selected_count),
        ("Batches generated", result.batch_count),
        ("Batch size", batch_size),
        ("Registry CSV", result.candidates_path),
        ("Targets file", result.targets_path),
        ("Sampling manifest", result.manifest_path),
    ):
        typer.echo(f"{label}: {value}")
