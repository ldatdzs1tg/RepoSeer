#!/usr/bin/env python3
"""Compatibility entrypoint into the shared profile CLI."""

import typer

from reposeer.cli.profile import profile_app


def main() -> None:
    typer.main.get_command(profile_app).main()


if __name__ == "__main__":
    main()
