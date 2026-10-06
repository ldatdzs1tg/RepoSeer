"""Provenance validator."""

import polars as pl


class ProvenanceValidator:
    def validate_provenance(self, df: pl.DataFrame) -> bool:
        return "source" in df.columns or "run_id" in df.columns
