#!/usr/bin/env python3
"""Script to run profiling on current processed datasets."""

from pathlib import Path

from reposeer.profiling.report import ProfilingReportGenerator
from reposeer.storage.paths import paths


def main() -> None:
    generator = ProfilingReportGenerator()
    tables = {
        "repositories": paths.processed_path("repositories"),
    }
    report = generator.generate_report(tables)
    out = Path("data/reports/data_quality/profile_report.json")
    generator.save_report(report, out)
    print(f"Profiling report written to {out}")


if __name__ == "__main__":
    main()
