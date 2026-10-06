"""Extracted article content contract."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class ArticleDocument(BaseSchema):
    """Clean extracted article text and metadata extracted via Trafilatura."""

    url: str
    title: str | None = None
    content: str
    author: str | None = None
    published_at: datetime | None = None
    source: str
    word_count: int = 0
    extracted_at: datetime = Field(default_factory=utc_now)
