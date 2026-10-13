"""CLI command for technology detection and search terms generation."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from reposeer.technology import detect_technologies

console = Console()
detect_tech_app = typer.Typer(
    help="Identify technology entities and generate external search terms.",
    context_settings={"allow_interspersed_args": True},
)


@detect_tech_app.callback(invoke_without_command=True)
def main(
    target: Annotated[
        str,
        typer.Argument(help="Manually supplied owner/repo (no fetch) or local directory"),
    ],
    readme_path: Annotated[
        Path | None,
        typer.Option("--readme", "-r", help="Path to README file", exists=True, dir_okay=False, readable=True),
    ] = None,
    requirements_path: Annotated[
        Path | None,
        typer.Option("--requirements", help="Path to requirements.txt file", exists=True, dir_okay=False, readable=True),
    ] = None,
    pyproject_path: Annotated[
        Path | None,
        typer.Option("--pyproject", help="Path to pyproject.toml file", exists=True, dir_okay=False, readable=True),
    ] = None,
    topics: Annotated[
        list[str] | None,
        typer.Option("--topic", "-t", help="Repository topic tag"),
    ] = None,
    pinned: Annotated[
        list[str] | None,
        typer.Option("--pinned", "-p", help="Pinned tech:<slug>, slug or alias"),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Output structured resolver JSON; entities are T-011 only with recorded evidence"),
    ] = False,
) -> None:
    """Detect technologies for a repository and generate search terms."""
    manifests: dict[str, str] = {}
    readme_content = None

    target_path = Path(target)
    local_input = target_path.is_dir()
    try:
        if local_input:
            for name in ("README.md", "README.rst", "README.txt", "readme.md"):
                candidate = target_path / name
                if candidate.is_file():
                    readme_content = candidate.read_text(encoding="utf-8")
                    break
            for name in ("requirements.txt", "pyproject.toml", "setup.cfg", "setup.py"):
                candidate = target_path / name
                if candidate.is_file():
                    manifests[name] = candidate.read_text(encoding="utf-8")
        for path, name in ((readme_path, None), (requirements_path, "requirements.txt"),
                           (pyproject_path, "pyproject.toml")):
            if path is None:
                continue
            content = path.read_text(encoding="utf-8")
            if name is None:
                readme_content = content
            else:
                manifests[name] = content
        result = detect_technologies(
            repo=None if local_input else target, readme_content=readme_content,
            manifest_files=manifests or None, topics=topics, pinned_signals=pinned,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    result.metadata.update(input_mode="local" if local_input else "manual", subject=target,
                           externally_retrieved=False)

    if output_json:
        console.print_json(result.model_dump_json(indent=2))
        return

    console.print(f"Technology resolution: {target}", markup=False)
    console.print("Input: local files" if local_input else "Input: manual signals (GitHub metadata was not fetched)")
    console.print(f"Canonical T-011 entities: {len(result.entities)}; resolved concepts: {len(result.resolved_specs)}")
    for diagnostic in result.diagnostics:
        console.print(f"Diagnostic: {diagnostic}", markup=False)

    # Table of detected technologies
    table = Table(title="Resolved Catalog Concepts", show_header=True, header_style="bold cyan")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Confidence")
    table.add_column("Sources")
    table.add_column("Technology ID")

    for entity in result.resolved_specs:
        conf = result.confidence_by_slug.get(entity.slug, 1.0)
        sources = ", ".join(result.detection_sources_by_slug.get(entity.slug, []))
        is_primary = result.primary_spec and result.primary_spec.slug == entity.slug
        name_display = f"{entity.name} (primary)" if is_primary else entity.name
        table.add_row(
            entity.slug,
            name_display,
            entity.category,
            f"{conf:.2f}",
            sources or "registry",
            f"tech:{entity.slug}",
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
