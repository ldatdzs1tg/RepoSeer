"""Dedicated start/resume CLI for durable GitHub activity runs."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from reposeer.exceptions import CollectionError
from reposeer.services.activity_batch_service import (
    ActivityBatchResult,
    ActivityBatchService,
    validate_budgets,
    validate_groups,
    validate_run_id,
    validate_targets,
)
from reposeer.storage.paths import StoragePaths

console = Console()
collect_activity_app = typer.Typer(help="Collect GitHub activity with durable page recovery.")


def _announce(run_id: str, checkpoint_path: Path) -> None:
    console.print(f"Run ID: {run_id}", markup=False)
    console.print(f"Checkpoint: {checkpoint_path}", markup=False)


def _display(result: ActivityBatchResult) -> None:
    for label, value in (
        ("Run ID", result.run_id), ("Status", result.status),
        ("Unique requested repositories", result.requested_count),
        ("Processed", result.processed_count), ("Excluded", result.excluded_count),
        ("Duplicate", result.duplicate_count), ("Failed", result.failed_count),
        ("Raw records stored", result.raw_stored_count),
        ("Resumable", "yes" if result.resumable else "no"),
        ("Checkpoint", result.checkpoint_path), ("Report", result.report_path),
    ):
        console.print(f"{label}: {value}", markup=False)
    table = Table("Repository", "Group", "Pages", "Stored", "Rejected", "Coverage")
    for group in result.groups:
        table.add_row(
            group["repo_full_name"], group["group"], str(group["pages_fetched"]),
            str(group["records_stored"]), str(group["records_rejected"]), group["status"],
        )
    console.print(table)


@collect_activity_app.command("start")
def start(
    repos_file: Path = typer.Option(..., "--repos-file", help="UTF-8 repository targets file"),
    groups: str = typer.Option("all", "--groups", help="all or comma-separated activity groups"),
    max_pages: int = typer.Option(5, "--max-pages"),
    max_records: int = typer.Option(500, "--max-records"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Create an immutable run and collect selected repository activity."""
    try:
        lines = repos_file.read_text(encoding="utf-8-sig").splitlines()
        targets = validate_targets([
            line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
        ])
        selected = validate_groups([group.strip() for group in groups.split(",")])
        validate_budgets(max_pages, max_records)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        console.print("Invalid activity input file, targets, groups or budgets.", style="bold red")
        raise typer.Exit(code=2) from error
    service = None
    try:
        service = ActivityBatchService(
            storage_paths=StoragePaths(data_dir) if data_dir is not None else None,
            on_run_started=_announce,
        )
        result = service.start(
            targets, groups=selected, max_pages_per_group=max_pages, max_records_per_group=max_records,
        )
        _display(result)
    except (CollectionError, OSError) as error:
        console.print("Activity run interrupted; use its run ID to resume after repair.", style="bold red")
        raise typer.Exit(code=1) from error
    except ValueError as error:
        console.print("Invalid activity collection configuration.", style="bold red")
        raise typer.Exit(code=2) from error
    finally:
        if service is not None:
            service.close()
    if result.status != "complete":
        raise typer.Exit(code=1)


@collect_activity_app.command("resume")
def resume(
    run_id: str = typer.Option(..., "--run-id", help="Original UUIDv4 run ID"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Resume the original immutable configuration without overriding budgets."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        console.print("Run ID must be a canonical UUIDv4.", style="bold red")
        raise typer.Exit(code=2) from error
    service = None
    try:
        service = ActivityBatchService(
            storage_paths=StoragePaths(data_dir) if data_dir is not None else None,
        )
        checkpoint = service.run_manager.checkpoints_dir / f"run_{run_id}.json"
        _announce(run_id, checkpoint)
        result = service.resume(run_id)
        _display(result)
    except (CollectionError, OSError) as error:
        console.print("Activity recovery failed; check checkpoint, journal and raw file integrity.", style="bold red")
        raise typer.Exit(code=1) from error
    except ValueError as error:
        console.print("Invalid activity collection configuration.", style="bold red")
        raise typer.Exit(code=2) from error
    finally:
        if service is not None:
            service.close()
    if result.status != "complete":
        raise typer.Exit(code=1)
