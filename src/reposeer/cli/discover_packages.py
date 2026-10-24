"""Generate evidenced PyPI target batches; collection is a separate command."""

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    FileSizeColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from reposeer.clients.pypi_index import DEFAULT_INDEX_BYTES, PackageDiscoveryError, ProgressEvent
from reposeer.pipelines.package_discovery import (
    PackageDiscoveryOptions,
    PackageDiscoveryPipeline,
    _load_names,
)
from reposeer.storage.paths import StoragePaths
from reposeer.storage.screening_mapping import (
    ScreeningMappingError,
    checked_mount_path,
    read_cumulative_mapping,
    recheck_master_metadata,
)

discover_packages_app = typer.Typer(
    help="Discover PyPI names and export reproducible target batches."
)


class _DiscoveryProgress(Progress):
    """Render one current phase; byte totals and ETA exist only for local reads."""

    def __init__(self, console: Console):
        super().__init__(console=console, refresh_per_second=4)
        self.current_stage = None
        self.current_task = None

    def refresh(self) -> None:
        # add_task normally renders immediately; let Live's 4 Hz loop render phases.
        if not self.live.is_started:
            super().refresh()

    def report(self, event: ProgressEvent) -> None:
        new_attempt = (
            event.stage == "downloading"
            and event.completed == 0
            and event.message is not None
            and event.message.startswith("Downloading")
        )
        if event.stage != self.current_stage or new_attempt:
            if self.current_task is not None:
                self.remove_task(self.current_task)
            columns = [SpinnerColumn(), TextColumn("{task.description}")]
            if event.stage == "downloading":
                columns += [FileSizeColumn(), TransferSpeedColumn()]
            elif event.stage in {"parsing", "reading_file"}:
                columns += [
                    BarColumn(),
                    TextColumn("{task.percentage:>5.1f}%"),
                    FileSizeColumn(),
                    TimeRemainingColumn(),
                    TextColumn("{task.fields[items]}"),
                ]
            elif event.stage == "exporting":
                columns += [BarColumn(), TextColumn("{task.completed:.0f} / {task.total:.0f}")]
            elif event.stage == "sampling":
                columns += [TextColumn("{task.fields[items]}")]
            self.columns = (*columns, TimeElapsedColumn())
            self.current_task = self.add_task(
                event.message
                or {
                    "parsing": "Parsing & normalization",
                    "exporting": "Exporting batches",
                }.get(event.stage, event.stage),
                total=event.total,
                items="",
            )
            self.current_stage = event.stage
        fields = {}
        if event.message is not None:
            fields["description"] = event.message
        if event.items_seen is not None:
            fields["items"] = f"{event.items_seen:,} names"
        if event.stage == "completed":
            fields.update(total=1, completed=1)
        else:
            fields["completed"] = event.completed
        self.update(self.current_task, **fields)


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
    drive_root: Path | None = typer.Option(
        None, "--drive-root", help="Shared RepoSeer-Data root with a verified cumulative mapping"
    ),
    no_progress: bool = typer.Option(
        False, "--no-progress", help="Disable terminal progress (automatic for non-TTY/CI)"
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
    console = Console(stderr=True)
    enabled = (
        not no_progress
        and sys.stderr.isatty()
        and console.is_terminal
        and not console.is_dumb_terminal
        and not os.environ.get("CI")
    )
    master_counts = None
    try:
        with ExitStack() as stack:
            run_options = {}
            if drive_root is not None:
                drive_root = checked_mount_path(drive_root)
                metadata, master_rows = read_cumulative_mapping(drive_root)
                screened = {row["package_name"] for row in master_rows.values()}
                additional = set()
                if exclude_file is not None:
                    _load_names(exclude_file, additional.add, kind="exclusion")
                excluded = screened | additional
                scratch = Path(
                    stack.enter_context(tempfile.TemporaryDirectory(prefix="reposeer-exclusions-"))
                )
                if scratch.resolve().is_relative_to(drive_root.resolve()):
                    raise ScreeningMappingError("exclusions_workspace_must_be_local")
                merged_file = scratch / "exclude.txt"
                with merged_file.open("w", encoding="utf-8", newline="") as stream:
                    for name in sorted(excluded):
                        stream.write(name + "\n")
                options = replace(options, exclude_file=merged_file)

                def validate_sample(names):
                    master_counts["duplicates"] = (
                        len(names) - len(set(names)) + len(set(names) & screened)
                    )
                    if set(names) & excluded:
                        raise ScreeningMappingError(
                            "selected_packages_overlap_master_or_exclusions"
                        )
                    recheck_master_metadata(drive_root, metadata)

                run_options["validate_sample"] = validate_sample
                master_counts = dict(
                    previously_screened=len(screened),
                    additional=len(additional - screened),
                    requested=count,
                    excluded=len(excluded),
                )
            progress = stack.enter_context(
                _DiscoveryProgress(console) if enabled else nullcontext()
            )
            result = PackageDiscoveryPipeline(
                progress=progress.report if progress is not None else None,
            ).run(options, **run_options)
            if drive_root is not None:
                recheck_master_metadata(drive_root, metadata)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    except (PackageDiscoveryError, ScreeningMappingError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    except (OSError, sqlite3.Error):
        typer.echo("package_discovery_storage_or_source_io_failed", err=True)
        raise typer.Exit(1) from None
    if master_counts is not None:
        for label, value in (
            ("Master mapping", "verified"),
            ("Previously screened packages", master_counts["previously_screened"]),
            ("Additional exclusions", master_counts["additional"]),
            ("Requested new packages", master_counts["requested"]),
            ("Selected new packages", result.selected_count),
            ("Excluded packages", master_counts["excluded"]),
            ("Duplicate packages in output", master_counts["duplicates"]),
        ):
            typer.echo(f"{label}: {value}")
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
