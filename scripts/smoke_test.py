#!/usr/bin/env python3
"""Live Smoke Test Script for RepoSeer.

Tests live HTTP requests to GitHub REST API, validates data schemas,
saves to raw JSONL, converts to columnar Parquet, and queries with DuckDB.
"""

import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from reposeer.clients.github import GitHubClient
from reposeer.collectors.github.repository import RepositoryCollector
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.query.duckdb import DuckDBQueryEngine
from reposeer.storage.raw.jsonl import JsonlStorage

console = Console()


def run_smoke_test(target: str = "octocat/Hello-World") -> None:
    parts = target.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1]

    console.print(
        Panel(
            f"[bold cyan]RepoSeer Live Smoke Test[/bold cyan]\nTarget: [yellow]{owner}/{repo}[/yellow]"
        )
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. Test live HTTP request against GitHub REST API
        console.print("[1/5] [bold blue]Curling live GitHub API...[/bold blue]")
        client = GitHubClient()
        try:
            raw_repo = client.get_repository(owner, repo)
            console.print(
                f"      ✓ Received HTTP 200: {raw_repo.get('full_name')} (Stars: {raw_repo.get('stargazers_count')})"
            )
        except Exception as e:
            console.print(f"      ✗ Failed to connect to GitHub API: {e}", style="bold red")
            sys.exit(1)
        finally:
            client.close()

        # 2. Validate with Pydantic v2 Schema Contract
        console.print("[2/5] [bold blue]Validating Pydantic v2 data contract...[/bold blue]")
        collector = RepositoryCollector()
        record = collector.collect(owner, repo)
        assert isinstance(record, RepositoryRecord)
        console.print(
            f"      ✓ Schema validated: RepositoryRecord(full_name='{record.full_name}', stars={record.stars})"
        )

        # 3. Write to Raw JSONL
        console.print("[3/5] [bold blue]Writing to Raw Storage (JSONL)...[/bold blue]")
        jsonl = JsonlStorage()
        raw_file = tmp_path / "raw.jsonl"
        jsonl.append_record(raw_file, record)
        assert raw_file.exists()
        console.print(
            f"      ✓ Raw JSONL persisted at: {raw_file} ({raw_file.stat().st_size} bytes)"
        )

        # 4. Transform to Processed Parquet
        console.print("[4/5] [bold blue]Converting to Processed Storage (Parquet)...[/bold blue]")
        parquet = ParquetStorage()
        parquet_file = tmp_path / "repositories.parquet"
        records = jsonl.read(raw_file)
        parquet.write(parquet_file, records)
        assert parquet_file.exists()
        console.print(
            f"      ✓ Processed Parquet created at: {parquet_file} ({parquet_file.stat().st_size} bytes)"
        )

        # 5. Query live database using DuckDB
        console.print("[5/5] [bold blue]Querying database via DuckDB SQL engine...[/bold blue]")
        duck = DuckDBQueryEngine()
        duck.register_parquet("repos", parquet_file)

        rows = duck.execute_sql(
            "SELECT full_name, stars, forks, default_branch, archived FROM repos"
        )
        duck.close()

        # Display results in table
        table = Table(title="DuckDB Query Results: SELECT * FROM repos")
        table.add_column("Repository", style="cyan")
        table.add_column("Stars", justify="right", style="green")
        table.add_column("Forks", justify="right")
        table.add_column("Branch", style="magenta")
        table.add_column("Archived", style="yellow")

        for r in rows:
            table.add_row(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]))

        console.print(table)
        console.print("[bold green]✓ ALL SMOKE TESTS PASSED SUCCESSFULLY![/bold green]\n")


if __name__ == "__main__":
    target_repo = sys.argv[1] if len(sys.argv) > 1 else "octocat/Hello-World"
    run_smoke_test(target_repo)
