"""Processed storage handler using Parquet and Polars."""

from pathlib import Path
from typing import Any

import polars as pl

from reposeer.storage.base import BaseStorage


class ParquetStorage(BaseStorage):
    """Parquet columnar storage for processed entity datasets."""

    def write(self, path: Path, data: pl.DataFrame | list[dict[str, Any]]) -> None:
        """Write records or DataFrame to Parquet with snappy/zstd compression."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, pl.DataFrame):
            df = data
        elif isinstance(data, list):
            df = pl.DataFrame(data) if data else pl.DataFrame()
        else:
            df = pl.DataFrame(list(data))
        df.write_parquet(path, compression="zstd")

    def read(self, path: Path) -> pl.DataFrame:
        """Read Parquet file into a Polars DataFrame."""
        if not path.exists():
            return pl.DataFrame()
        return pl.read_parquet(path)

    def scan(self, path: Path) -> pl.LazyFrame:
        """Scan Parquet file for lazy evaluation."""
        if not path.exists():
            return pl.LazyFrame()
        return pl.scan_parquet(path)
