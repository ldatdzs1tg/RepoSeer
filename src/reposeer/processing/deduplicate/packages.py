"""Package records deduplication."""

import polars as pl


class PackageDeduplicator:
    def deduplicate(self, df: pl.DataFrame) -> pl.DataFrame:
        if df.is_empty() or "name" not in df.columns:
            return df
        sub = ["ecosystem", "name"] if "ecosystem" in df.columns else ["name"]
        return df.unique(subset=sub, keep="last")
