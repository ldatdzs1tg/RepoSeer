"""Completeness and missing value analysis."""

import polars as pl


class CompletenessProfiler:
    def compute_missing_rate(self, df: pl.DataFrame) -> dict[str, float]:
        if df.is_empty():
            return {}
        total = len(df)
        return {col: float(df[col].null_count()) / total for col in df.columns}
