"""CLI entry point for CORE analysis of a prepared cutoff feature package."""

from pathlib import Path
from typing import Annotated

import typer

from reposeer.analysis.core import CoreFeaturePackage, analyze_core


def analyze(
    feature_package: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
    model: Annotated[Path, typer.Option(dir_okay=False, help="Frozen Model A JSON artifact.")],
    mode: Annotated[str, typer.Option(help="Requested mode, CORE or ENHANCED.")] = "CORE",
    purpose: Annotated[
        str, typer.Option(help="current_inference or retrospective_analysis.")
    ] = "retrospective_analysis",
) -> None:
    """Return a validated analysis report for one cutoff feature package."""
    try:
        package = CoreFeaturePackage.model_validate_json(feature_package.read_bytes())
        report = analyze_core(package, model, mode_requested=mode, purpose=purpose)
    except (OSError, ValueError) as error:
        typer.echo(f"Analyze stopped. {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(report.model_dump_json(indent=2))
    if report.result_status == "failed":
        raise typer.Exit(code=1)
