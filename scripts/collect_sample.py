#!/usr/bin/env python3
"""Sample script to run collection on a sample repository."""

import sys

from reposeer.pipelines.full import FullPipeline


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "pallets/flask"
    print(f"Collecting sample repository data for: {target}")
    pipeline = FullPipeline()
    pipeline.run(target)
    print("Done!")


if __name__ == "__main__":
    main()
