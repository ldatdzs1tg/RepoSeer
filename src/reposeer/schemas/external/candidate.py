"""External discovery candidate contract."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class ExternalCandidate(BaseSchema):
    """External article, post, or mention identified during discovery."""

    title: str
    url: str
    source: str  # "hackernews", "rss", "newsapi"
    publication_date: datetime | None = None
    query: str | None = None
    score: float | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
