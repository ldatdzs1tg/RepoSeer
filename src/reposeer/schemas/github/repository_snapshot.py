"""Point-in-time snapshot schema for tracking repository metrics over time."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class RepositorySnapshot(BaseSchema):
    """Snapshot of metrics for time-series / longitudinal analysis."""

    repo_full_name: str
    snapshot_at: datetime = Field(default_factory=utc_now)
    stars: int = 0
    forks: int = 0
    watchers: int = 0
    open_issues: int = 0
    size_kb: int = 0
