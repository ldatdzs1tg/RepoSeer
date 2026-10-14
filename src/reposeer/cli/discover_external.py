"""CLI command for external discovery and candidate collection (Task T-016)."""

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from reposeer.pipelines.external import ExternalPipeline
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.raw.jsonl import JsonlStorage

console = Console()
discover_external_app = typer.Typer(
    help="Discover external articles, discussions, and candidates (Task T-016).",
    context_settings={"allow_interspersed_args": True},
)


@discover_external_app.callback(invoke_without_command=True)
def main(
    target: Annotated[
        str | None,
        typer.Argument(
            help="Target repository 'owner/repo', URL, or package 'pypi:name' (or omit if using --from-stored)"
        ),
    ] = None,
    from_stored: Annotated[
        bool,
        typer.Option("--from-stored", help="Run discovery for stored raw repositories in data/raw/github/repositories.jsonl"),
    ] = False,
    cutoff_at: Annotated[
        str | None,
        typer.Option("--cutoff-at", help="UTC cutoff timestamp ISO-8601 (e.g. 2024-03-01T00:00:00Z)"),
    ] = None,
    window_days: Annotated[
        int | None,
        typer.Option("--window-days", help="Lookback window in days from cutoff_at"),
    ] = None,
    max_pages: Annotated[
        int,
        typer.Option("--max-pages", help="Max pages per search query"),
    ] = 5,
    max_records: Annotated[
        int,
        typer.Option("--max-records", help="Max records per run"),
    ] = 100,
    sources: Annotated[
        str,
        typer.Option("--sources", "-s", help="Comma-separated discovery sources (e.g. hackernews,devto)"),
    ] = "hackernews",
    historical: Annotated[
        bool,
        typer.Option("--historical", help="Enable historical mode (guards sources lacking window support)"),
    ] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Base storage directory"),
    ] = None,
) -> None:
    """Run external multi-query discovery with cutoff safety and canonical raw persistence."""
    sp = StoragePaths(data_dir) if data_dir else paths
    storage = JsonlStorage()
    source_list = [s.strip() for s in sources.split(",") if s.strip()]

    pipeline = ExternalPipeline(storage_paths=sp, storage=storage)

    targets_to_run: list[Any] = []
    if from_stored:
        raw_repo_path = sp.raw_path("github", "repositories")
        records = storage.read_contract_records(raw_repo_path)
        if not records:
            console.print(f"No stored raw repositories found at {raw_repo_path}.", style="bold yellow")
            raise typer.Exit(code=1)
        targets_to_run.extend(records)
    elif target is not None:
        targets_to_run.append(target)
    else:
        console.print("Provide a repository target or specify --from-stored.", style="bold red")
        raise typer.Exit(code=1)

    try:
        for subj in targets_to_run:
            display_name = subj if isinstance(subj, str) else getattr(subj, "source_url", str(subj))
            console.print(f"Discovering external candidates for: {display_name}", style="bold cyan")
            res = pipeline.discover(
                subj,
                cutoff_at=cutoff_at,
                window_days=window_days,
                max_pages=max_pages,
                max_records=max_records,
                sources=source_list,
                historical_mode=historical,
            )

            manifest = res.discovery_manifest
            console.print(f"Run ID: [bold]{res.run_id}[/bold]")
            console.print(f"Query set hash: {manifest['query_set_hash']}")
            console.print(f"Records observed / emitted: {res.collection_result.metadata.records_emitted}")
            console.print(f"Records rejected: {res.collection_result.metadata.records_rejected}")
            console.print(f"Completeness status: {manifest['completeness_status']}")
            console.print(f"Pagination complete: {manifest['pagination_complete']}")

            table = Table(title="Per-Query Discovery Statistics", show_header=True, header_style="bold magenta")
            table.add_column("Query", style="yellow")
            table.add_column("Intent", width=14)
            table.add_column("Platform", width=12)
            table.add_column("Status", width=10)
            table.add_column("Candidates", justify="right", width=10)

            for pq in manifest.get("per_query_stats", [])[:15]:
                table.add_row(
                    pq.get("query", ""),
                    pq.get("intent", ""),
                    pq.get("platform", ""),
                    pq.get("status", ""),
                    str(pq.get("candidates_found", 0)),
                )
            console.print(table)
            console.print(f"Persisted to: {res.output_path}\n", style="green")

    finally:
        pipeline.close()
