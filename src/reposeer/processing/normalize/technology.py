"""Technology normalization transformer."""

import polars as pl

from reposeer.technology.aliases import canonicalize_technology


class TechnologyNormalizer:
    """Applies alias normalization across technology datasets using Polars."""

    def normalize_dataframe(self, df: pl.DataFrame, col_name: str = "name") -> pl.DataFrame:
        if col_name not in df.columns:
            return df
        # Apply canonicalization
        return df.with_columns(
            pl.col(col_name)
            .map_elements(canonicalize_technology, return_dtype=pl.String)
            .alias(col_name)
        )
