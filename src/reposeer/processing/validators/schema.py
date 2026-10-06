"""Schema validator."""

import polars as pl


class SchemaValidator:
    def validate_columns(self, df: pl.DataFrame, required_columns: list[str]) -> bool:
        missing = [col for col in required_columns if col not in df.columns]
        return len(missing) == 0
