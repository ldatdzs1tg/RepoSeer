"""Comprehensive Data Quality and Profiling Report Generator using DuckDB and Polars."""

import json
from pathlib import Path
from typing import Any

import polars as pl

from reposeer.profiling.completeness import CompletenessProfiler
from reposeer.profiling.dataset import DatasetProfiler
from reposeer.storage.query.duckdb import DuckDBQueryEngine


class ProfilingReportGenerator:
    """Generates structured profiling reports across raw and processed datasets."""

    def __init__(self, duckdb_engine: DuckDBQueryEngine | None = None):
        self.duckdb = duckdb_engine or DuckDBQueryEngine()
        self.dataset_profiler = DatasetProfiler()
        self.completeness_profiler = CompletenessProfiler()

    def generate_report(self, parquet_files: dict[str, Path]) -> dict[str, Any]:
        """Generate comprehensive report summarizing all tables."""
        report: dict[str, Any] = {"datasets": {}}
        for name, path in parquet_files.items():
            if not path.exists():
                continue
            df = pl.read_parquet(path)
            stats = self.dataset_profiler.profile_dataframe(df, name=name)
            missing = self.completeness_profiler.compute_missing_rate(df)
            report["datasets"][name] = {
                "stats": stats,
                "missing_rate": missing,
            }
        return report

    def save_report(self, report: dict[str, Any], output_path: Path) -> None:
        """Persist report to JSON file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
