"""Feature builder orchestrating feature extraction across subsystems."""

import polars as pl


class FeatureBuilder:
    """Builds unified dataset features for ML and analytics."""

    def build_snapshot_features(
        self,
        repos_df: pl.DataFrame,
        activity_df: pl.DataFrame | None = None,
    ) -> pl.DataFrame:
        """Combine repository metadata with activity aggregations."""
        if repos_df.is_empty():
            return pl.DataFrame()
        return repos_df
