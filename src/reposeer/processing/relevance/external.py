"""Relevance filter for external articles and mentions."""

import polars as pl


class RelevanceFilter:
    """Filters out noisy, off-topic, or irrelevant news."""

    def filter_by_keywords(
        self, df: pl.DataFrame, text_col: str, keywords: list[str]
    ) -> pl.DataFrame:
        if df.is_empty() or text_col not in df.columns or not keywords:
            return df
        pattern = "(?i)" + "|".join(keywords)
        return df.filter(pl.col(text_col).str.contains(pattern))
