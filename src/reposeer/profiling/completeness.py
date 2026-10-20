"""Completeness metrics preserve undefined rates for empty datasets."""

import polars as pl


class CompletenessProfiler:
    def compute_missing_rate(self, df: pl.DataFrame) -> dict[str, float | None]:
        return {column: df[column].null_count() / df.height if df.height else None
                for column in df.columns}
