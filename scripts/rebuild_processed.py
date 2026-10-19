#!/usr/bin/env python3
"""Rebuild via the same offline processing implementation/options as reposeer process."""

from reposeer.cli.process import process_app


def main() -> None:
    process_app()


if __name__ == "__main__":
    main()
