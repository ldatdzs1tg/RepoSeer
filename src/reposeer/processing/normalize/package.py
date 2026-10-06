"""Package normalization transformer."""

import polars as pl


class PackageNormalizer:
    def to_dataframe(self, records: list) -> pl.DataFrame:
        if not records:
            return pl.DataFrame()
        dicts = [r.model_dump() if hasattr(r, "model_dump") else r for r in records]
        return pl.DataFrame(dicts)
