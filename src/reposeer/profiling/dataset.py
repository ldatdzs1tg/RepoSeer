"""Dataset summary profiler using Polars."""

from typing import Any

import polars as pl


class DatasetProfiler:
    """Computes summary statistics and profiling metrics on tabular datasets."""

    def profile_dataframe(self, df: pl.DataFrame, name: str = "dataset") -> dict[str, Any]:
        if df.is_empty():
            return {
                "name": name,
                "row_count": 0,
                "column_count": len(df.columns),
                "columns": df.columns,
                "null_counts": {},
            }

        null_counts = {col: df[col].null_count() for col in df.columns}
        return {
            "name": name,
            "row_count": len(df),
            "column_count": len(df.columns),
            "columns": df.columns,
            "null_counts": null_counts,
        }
