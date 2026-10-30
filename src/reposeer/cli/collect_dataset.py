"""Bounded collection integration with explicit live and offline stored modes."""

from pathlib import Path

import typer

from reposeer.exceptions import CollectionError, StorageError
from reposeer.pipelines.collection import CollectionPipeline, parse_targets, validate_configuration
from reposeer.storage.paths import StoragePaths

collect_dataset_app = typer.Typer(help="Integrate collection, saved-raw processing and data profiling.")


@collect_dataset_app.callback(invoke_without_command=True)
def main(
    targets_file: Path = typer.Option(..., "--targets-file", help="UTF-8 targets, one per line"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    mode: str = typer.Option("live", "--mode", help="live or stored"),
    groups: str = typer.Option("all", "--groups"),
    max_pages: int = typer.Option(2, "--max-pages"),
    max_records: int = typer.Option(200, "--max-records"),
    discovery_sources: str = typer.Option("hackernews", "--discovery-sources"),
    discovery_max_pages: int = typer.Option(1, "--discovery-max-pages"),
    discovery_max_records: int = typer.Option(30, "--discovery-max-records"),
    max_candidates: int = typer.Option(15, "--max-candidates"),
    policy: str | None = typer.Option(None, "--policy", help="Explicit distributed-v2 setup gate"),
    cutoff_at: str | None = typer.Option(None, "--cutoff-at", help="Stored diagnostics only"),
) -> None:
    if policy is not None:
        if policy != "distributed-v2" or mode != "live":
            raise typer.BadParameter("distributed-v2 requires live mode")
        from reposeer.collection.provider_preflight import check_providers

        preflight = check_providers()
        if not preflight["ready"]:
            import json

            typer.echo(json.dumps(preflight), err=True)
            raise typer.Exit(1)
        max_pages, max_records = 4, 300
        discovery_sources = "hackernews,devto,stackexchange,rss,news"
        discovery_max_pages, discovery_max_records, max_candidates = 3, 150, 20
    options = dict(mode=mode, groups=[value.strip() for value in groups.split(",")],
                   max_pages=max_pages, max_records=max_records,
                   discovery_sources=[value.strip() for value in discovery_sources.split(",")],
                   discovery_max_pages=discovery_max_pages, discovery_max_records=discovery_max_records,
                   max_candidates=max_candidates, cutoff_at=cutoff_at)
    try:
        targets = [line.strip() for line in targets_file.read_text(encoding="utf-8-sig").splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
        parse_targets(targets)
        validate_configuration(**options)
    except (OSError, UnicodeError, ValueError, TypeError):
        typer.echo("Invalid targets file, targets, mode, groups or budgets. Cutoff requires stored diagnostics.", err=True)
        raise typer.Exit(2) from None
    try:
        result = CollectionPipeline(StoragePaths(data_dir)).run(targets, **options)
    except (OSError, ValueError, TypeError, CollectionError, StorageError):
        typer.echo("Integration interrupted; inspect its saved manifest and child checkpoints.", err=True)
        raise typer.Exit(1) from None
    for label, value in (
        ("Integration run ID", result.integration_run_id), ("Overall status", result.status),
        ("Requested targets", result.targets_requested), ("Resolved targets", result.targets_resolved),
        ("Repositories collected", result.repositories_collected),
        ("GitHub activity run ID", ", ".join(result.child_run_ids.get("github_activity", [])) or "none"),
        ("Discovery runs", len(result.child_run_ids.get("external_discovery", []))),
        ("Crawl runs", len(result.child_run_ids.get("external_crawl", []))),
        ("Processing status", result.processing_status), ("Manifest", result.manifest_path),
        ("Collection profile", result.collection_report_path), ("Data profile", result.data_profile_path),
    ):
        typer.echo(f"{label}: {value}")
    typer.echo("Limitations: source coverage not_verified; historical validity unverified; single writer.")
    if result.status != "complete":
        raise typer.Exit(1)
