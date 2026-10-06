"""External article and candidate normalizer."""

import re

import polars as pl


class ExternalNormalizer:
    def normalize_urls(self, df: pl.DataFrame, url_col: str = "url") -> pl.DataFrame:
        """Strip tracking query parameters (utm_*, etc.) from URLs."""
        if url_col not in df.columns:
            return df

        def clean_url(url: str) -> str:
            if not url:
                return ""
            cleaned = re.sub(r"[?&]utm_[^&]+", "", url)
            cleaned = cleaned.rstrip("?&")
            return cleaned.rstrip("/")

        return df.with_columns(
            pl.col(url_col).map_elements(clean_url, return_dtype=pl.String).alias(url_col)
        )
