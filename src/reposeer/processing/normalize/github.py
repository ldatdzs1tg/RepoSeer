"""GitHub normalization transformer using Polars."""

import polars as pl

from reposeer.schemas.github.repository import RepositoryRecord


class GitHubNormalizer:
    """Transforms raw GitHub records into structured normalized Polars DataFrames."""

    def to_dataframe(self, records: list[RepositoryRecord] | list[dict]) -> pl.DataFrame:
        if not records:
            return pl.DataFrame()
        dicts = [r.model_dump() if hasattr(r, "model_dump") else r for r in records]
        df = pl.DataFrame(dicts)
        return df
