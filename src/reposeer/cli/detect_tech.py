"""CLI command for technology detection and search terms generation."""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from reposeer.technology import detect_technologies

console = Console()
detect_tech_app = typer.Typer(help="Identify technology entities and generate external search terms.")


@detect_tech_app.callback(invoke_without_command=True)
def main(
    target: Annotated[
        str,
        typer.Argument(help="Repository target 'owner/repo' or local directory"),
    ],
    readme_path: Annotated[
        Path | None,
        typer.Option("--readme", "-r", help="Path to README file"),
    ] = None,
    requirements_path: Annotated[
        Path | None,
        typer.Option("--requirements", help="Path to requirements.txt file"),
    ] = None,
    pyproject_path: Annotated[
        Path | None,
        typer.Option("--pyproject", help="Path to pyproject.toml file"),
    ] = None,
    topics: Annotated[
        list[str] | None,
        typer.Option("--topic", "-t", help="Repository topic tag"),
    ] = None,
    pinned: Annotated[
        list[str] | None,
        typer.Option("--pinned", "-p", help="Pinned technology slug/alias"),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Output full detection result as JSON conforming to T-011"),
    ] = False,
) -> None:
    """Detect technologies for a repository and generate search terms."""
    manifests: dict[str, str] = {}
    readme_content = None

    # Check if target is a local directory
    target_path = Path(target)
    if target_path.is_dir():
        for r_name in ["README.md", "README.rst", "README.txt", "readme.md"]:
            candidate_r = target_path / r_name
            if candidate_r.is_file():
                readme_content = candidate_r.read_text(encoding="utf-8", errors="ignore")
                break
        req_file = target_path / "requirements.txt"
        if req_file.is_file():
            manifests["requirements.txt"] = req_file.read_text(encoding="utf-8", errors="ignore")
        pyp_file = target_path / "pyproject.toml"
        if pyp_file.is_file():
            manifests["pyproject.toml"] = pyp_file.read_text(encoding="utf-8", errors="ignore")

    # Override with explicit paths if provided
    if readme_path and readme_path.is_file():
        readme_content = readme_path.read_text(encoding="utf-8", errors="ignore")
    if requirements_path and requirements_path.is_file():
        manifests["requirements.txt"] = requirements_path.read_text(encoding="utf-8", errors="ignore")
    if pyproject_path and pyproject_path.is_file():
        manifests["pyproject.toml"] = pyproject_path.read_text(encoding="utf-8", errors="ignore")

    result = detect_technologies(
        repo=target,
        readme_content=readme_content,
        manifest_files=manifests if manifests else None,
        topics=topics,
        pinned_signals=pinned,
    )

    if output_json:
        console.print_json(result.model_dump_json(indent=2))
        return

    console.print(f"\n[bold green]Technology Entities Detection (T-011 Schema)[/bold green]: [bold]{target}[/bold]\n")

    # Table of detected technologies
    table = Table(title="Detected Technology Entities", show_header=True, header_style="bold cyan")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Confidence")
    table.add_column("Sources")
    table.add_column("Technology ID")

    for entity in result.entities:
        conf = result.confidence_by_slug.get(entity.slug, 1.0)
        sources = ", ".join(result.detection_sources_by_slug.get(entity.slug, []))
        is_primary = result.primary_technology and result.primary_technology.slug == entity.slug
        name_display = f"{entity.name} (primary)" if is_primary else entity.name
        table.add_row(
            entity.slug,
            name_display,
            entity.category,
            f"{conf:.2f}",
            sources or "registry",
            entity.technology_id,
        )

    console.print(table)
    console.print()

    # Table of generated search terms
    st_table = Table(title="Generated External Search Terms", show_header=True, header_style="bold magenta")
    st_table.add_column("Prio", justify="center", width=5)
    st_table.add_column("Intent", width=14)
    st_table.add_column("Platform", width=12)
    st_table.add_column("Query", style="yellow")

    for term in result.search_terms[:15]:
        st_table.add_row(
            str(term.priority),
            term.intent,
            term.platform,
            term.query,
        )

    console.print(st_table)
    if len(result.search_terms) > 15:
        console.print(f"[dim]... and {len(result.search_terms) - 15} more search terms.[/dim]")
    console.print()
