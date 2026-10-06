"""CLI profile command."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

console = Console()
profile_app = typer.Typer(help="Profile datasets and check data quality using Polars and DuckDB.")


@profile_app.callback(invoke_without_command=True)
def main(
    output: Path = typer.Option(
        Path("data/reports/data_quality/profile_report.json"),
        "--output",
        "-o",
        help="Report output file",
    ),
) -> None:
    """Generate data profiling and completeness metrics."""
    console.print("[bold cyan]Running dataset profiling...[/bold cyan]")
    from reposeer.profiling.report import ProfilingReportGenerator
    from reposeer.storage.paths import paths

    generator = ProfilingReportGenerator()
    parquet_files = {
        "repositories": paths.processed_path("repositories"),
    }
    report = generator.generate_report(parquet_files)
    generator.save_report(report, output)

    table = Table(title="Dataset Profiling Summary")
    table.add_column("Dataset", style="bold")
    table.add_column("Rows")
    table.add_column("Columns")

    for name, data in report.get("datasets", {}).items():
        stats = data.get("stats", {})
        table.add_row(name, str(stats.get("row_count", 0)), str(stats.get("column_count", 0)))

    console.print(table)
    console.print(f"[bold green]✓ Profiling report saved to {output}[/bold green]")
