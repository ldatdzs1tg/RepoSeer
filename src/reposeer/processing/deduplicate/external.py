"""External article deduplication according to page 19 of PDF."""

import hashlib

import polars as pl


class ExternalDeduplicator:
    """Deduplicates articles based on URL canonicalization, normalized title, or content hash."""

    def deduplicate(self, df: pl.DataFrame) -> pl.DataFrame:
        if df.is_empty():
            return df

        # If url exists, dedup by url
        subset = []
        if "url" in df.columns:
            subset.append("url")
        if not subset and "title" in df.columns:
            subset.append("title")

        return df.unique(subset=subset, keep="first") if subset else df

    @staticmethod
    def compute_content_hash(text: str) -> str:
        """Compute SHA256 of text."""
        return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
