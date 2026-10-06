"""Date and temporal coverage analysis."""

from typing import Any

import polars as pl


class CoverageProfiler:
    def get_date_coverage(self, df: pl.DataFrame, date_col: str) -> dict[str, Any]:
        if df.is_empty() or date_col not in df.columns:
            return {"min_date": None, "max_date": None}
        return {
            "min_date": str(df[date_col].min()),
            "max_date": str(df[date_col].max()),
        }
