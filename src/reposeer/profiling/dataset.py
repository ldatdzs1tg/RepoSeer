"""Physical dataset statistics; freshness never establishes historical validity."""

from datetime import UTC, datetime
from typing import Any

import polars as pl

from reposeer.contracts.identity import checked_timestamp
from reposeer.profiling.completeness import CompletenessProfiler


class DatasetProfiler:
    def profile_dataframe(self, df: pl.DataFrame, name: str = "dataset", *,
                          now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        counts = {column: df[column].null_count() for column in df.columns}
        rates = CompletenessProfiler().compute_missing_rate(df)
        result = dict(name=name, row_count=df.height, column_count=df.width, columns=df.columns,
                      schema={key: str(value) for key, value in df.schema.items()},
                      null_counts=counts, null_rates=rates)
        if "retrieved_at" in df.columns:
            timestamps = df["retrieved_at"].drop_nulls().to_list()
            earliest, latest = min(timestamps, default=None), max(timestamps, default=None)
            result.update(min_retrieved_at=earliest, max_retrieved_at=latest,
                          latest_observation_age_seconds=(
                              (now - datetime.fromisoformat(checked_timestamp(latest))).total_seconds() if latest else None))
        if "available_at" in df.columns:
            result.update(available_at_null_count=counts["available_at"],
                          available_at_null_rate=rates["available_at"])
        return result
