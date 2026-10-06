"""Analytical feature record for modeling repository health."""

from datetime import date

from reposeer.schemas.common import BaseSchema


class RepositoryPackageSnapshot(BaseSchema):
    """Consolidated feature vector for repository health & trend analysis."""

    repo_full_name: str
    snapshot_date: date
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    commits_last_30d: int = 0
    commits_last_90d: int = 0
    days_since_last_commit: int | None = None
    active_contributors_last_90d: int = 0
    is_archived: bool = False
    has_recent_release: bool = False
    article_mentions_30d: int = 0
    known_vulnerabilities_count: int = 0
