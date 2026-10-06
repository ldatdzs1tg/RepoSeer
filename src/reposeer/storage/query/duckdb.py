"""DuckDB analytics and query engine layer."""

from pathlib import Path
from typing import Any

import duckdb
import polars as pl


class DuckDBQueryEngine:
    """Embedded DuckDB engine for high-performance SQL analytics on Parquet."""

    def __init__(self, db_path: str = ":memory:"):
        self.conn = duckdb.connect(db_path)

    def execute_sql(self, query: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """Run SQL query and return rows."""
        cursor = self.conn.execute(query, params or [])
        return cursor.fetchall()

    def query_to_polars(self, query: str, params: list[Any] | None = None) -> pl.DataFrame:
        """Run SQL and return result as Polars DataFrame."""
        arrow_table = self.conn.execute(query, params or []).arrow()
        return pl.from_arrow(arrow_table)  # type: ignore[return-value]

    def register_parquet(self, view_name: str, parquet_path: Path | str) -> None:
        """Register a parquet file or directory as a queryable SQL view."""
        self.conn.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet('{parquet_path}')"
        )

    def close(self) -> None:
        """Close connection."""
        self.conn.close()
