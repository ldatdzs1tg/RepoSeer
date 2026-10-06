"""GitHub pull request schema."""

from datetime import datetime

from reposeer.schemas.common import BaseSchema


class PullRequestRecord(BaseSchema):
    """Pull request schema capturing collaboration velocity."""

    number: int
    repo_full_name: str
    title: str
    state: str = "open"
    created_at: datetime
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    is_merged: bool = False
    comments_count: int = 0
