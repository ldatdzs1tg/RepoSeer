"""GitHub contributor schema."""

from reposeer.schemas.common import BaseSchema


class ContributorRecord(BaseSchema):
    """Contributor profile and contribution weight."""

    login: str
    repo_full_name: str
    contributions: int = 0
    contributor_type: str = "User"
    avatar_url: str | None = None
