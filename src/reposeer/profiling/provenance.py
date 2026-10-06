"""Provenance profiler."""

import polars as pl


class ProvenanceProfiler:
    def profile_sources(self, df: pl.DataFrame) -> dict[str, int]:
        if df.is_empty() or "source" not in df.columns:
            return {}
        counts = df.group_by("source").len()
        return dict(zip(counts["source"].to_list(), counts["len"].to_list(), strict=False))
