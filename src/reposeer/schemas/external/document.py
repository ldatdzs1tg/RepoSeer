"""Extracted article content contract."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from reposeer.contracts.identity import format_utc_timestamp, normalize_article_text
from reposeer.contracts.models import HttpUrl, NonEmpty
from reposeer.schemas.common import utc_now


def validate_publication_metadata(published_at, published_date, precision, updated_at, observed_at):
    """Shared extraction invariants, matching canonical Article time semantics."""
    if precision == "timestamp":
        if published_at is None or published_date != published_at[:10]:
            raise ValueError("timestamp publication needs a timestamp and matching UTC date")
    elif precision == "date":
        if published_at is not None or published_date is None:
            raise ValueError("date publication requires a date and null timestamp")
    elif published_at is not None or published_date is not None:
        raise ValueError("unknown publication requires null date and timestamp")
    if published_date is not None:
        date.fromisoformat(published_date)
        if published_date > observed_at[:10]:
            raise ValueError("publication date is after observation")
    if published_at is not None and published_at > observed_at:
        raise ValueError("publication timestamp is after observation")
    if updated_at is not None and (
        updated_at > observed_at or (published_at is not None and updated_at < published_at)
    ):
        raise ValueError("edit timestamp is outside publication/observation bounds")


class ArticleDocument(BaseModel):
    """Clean extracted article text and metadata extracted via Trafilatura."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    url: HttpUrl
    title: NonEmpty | None = None
    content: NonEmpty
    author: NonEmpty | None = None
    published_at: AwareDatetime | None = None
    published_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    published_precision: Literal["timestamp", "date", "unknown"] = "unknown"
    updated_at: AwareDatetime | None = None
    source: NonEmpty
    word_count: int = Field(default=0, ge=0)
    extracted_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def coherent(self):
        validate_publication_metadata(
            format_utc_timestamp(self.published_at) if self.published_at else None,
            self.published_date,
            self.published_precision,
            format_utc_timestamp(self.updated_at) if self.updated_at else None,
            format_utc_timestamp(self.extracted_at),
        )
        if self.content != normalize_article_text(self.content):
            raise ValueError("content must be NFC/LF with outer whitespace stripped")
        if self.word_count != len(self.content.split()):
            raise ValueError("word_count must match extracted text")
        return self
