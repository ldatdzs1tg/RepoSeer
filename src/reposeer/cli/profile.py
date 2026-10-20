"""Standalone profiling of all T-019 datasets under one storage root."""

from pathlib import Path

import polars as pl
import typer

from reposeer.profiling.report import ProfilingReportGenerator
from reposeer.storage.paths import StoragePaths

profile_app = typer.Typer(help="Profile existing processed datasets without collection.")


@profile_app.callback(invoke_without_command=True)
def main(
    output: Path | None = typer.Option(None, "--output", "-o", help="Report output file"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Storage root"),
) -> None:
    paths = StoragePaths(data_dir)
    output = output or paths.reports_dir / "data_quality" / "profile_report.json"
    generator = ProfilingReportGenerator()
    try:
        report = generator.profile_storage(paths)
        generator.save_report(report, output)
    except (OSError, ValueError, pl.exceptions.PolarsError):
        typer.echo("Cannot read processed datasets or persist profile.", err=True)
        raise typer.Exit(1) from None
    for name, data in report["datasets"].items():
        typer.echo(f"{name}: {data['row_count'] if data['present'] else 'missing'}")
    typer.echo(f"Profiling report saved to {output}")
