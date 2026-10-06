"""Feature set storage handler."""

from pathlib import Path

import polars as pl

from reposeer.storage.processed.parquet import ParquetStorage


class FeatureStorage(ParquetStorage):
    """Parquet storage specialized for feature matrices and snapshots."""

    def save_features(self, path: Path, features_df: pl.DataFrame) -> None:
        """Save feature DataFrame."""
        self.write(path, features_df)

    def load_features(self, path: Path) -> pl.DataFrame:
        """Load feature DataFrame."""
        return self.read(path)
