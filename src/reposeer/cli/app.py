"""Main Typer CLI entrypoint for RepoSeer."""

import typer

from reposeer import __version__
from reposeer.cli.build_features import build_features_app
from reposeer.cli.collect import collect_app
from reposeer.cli.process import process_app
from reposeer.cli.profile import profile_app

app = typer.Typer(
    name="reposeer",
    help="RepoSeer: Open-Source Repository and Ecosystem Intelligence CLI",
    add_completion=False,
)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"RepoSeer version: {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Show RepoSeer version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """RepoSeer pipeline runner and dataset manager."""
    pass


app.add_typer(collect_app, name="collect")
app.add_typer(process_app, name="process")
app.add_typer(profile_app, name="profile")
app.add_typer(build_features_app, name="build-features")


if __name__ == "__main__":
    app()
