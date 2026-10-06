"""GitHub issue tracking schema."""

from datetime import datetime

from reposeer.schemas.common import BaseSchema


class IssueRecord(BaseSchema):
    """Issue record for tracking maintenance and community health."""

    number: int
    repo_full_name: str
    title: str
    state: str = "open"  # "open" or "closed"
    created_at: datetime
    closed_at: datetime | None = None
    comments_count: int = 0
    is_pull_request: bool = False
