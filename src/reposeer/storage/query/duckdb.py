"""DuckDB analytics and query engine layer."""

import re
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
        """Register an existing Parquet file as a queryable SQL view."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", view_name):
            raise ValueError("invalid_sql_view_identifier")
        if not str(parquet_path).strip():
            raise ValueError("empty_parquet_path")
        path = Path(parquet_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        self.conn.from_parquet(str(path)).create_view(view_name, replace=True)

    def close(self) -> None:
        """Close connection."""
        self.conn.close()
