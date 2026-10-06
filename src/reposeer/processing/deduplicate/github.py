"""GitHub records deduplication."""

import polars as pl


class GitHubDeduplicator:
    def deduplicate_repos(self, df: pl.DataFrame) -> pl.DataFrame:
        if df.is_empty() or "full_name" not in df.columns:
            return df
        return df.unique(subset=["full_name"], keep="last")

    def deduplicate_commits(self, df: pl.DataFrame) -> pl.DataFrame:
        if df.is_empty() or "sha" not in df.columns:
            return df
        return df.unique(subset=["sha"], keep="last")
