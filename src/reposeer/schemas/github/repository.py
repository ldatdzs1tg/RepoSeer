"""GitHub repository metadata schema."""

from pydantic import Field

from reposeer.schemas.common import BaseSchema, TimestampMixin


class RepositoryRecord(BaseSchema, TimestampMixin):
    """Normalized metadata record for a GitHub repository."""

    id: int | None = None
    owner: str
    name: str
    full_name: str
    description: str | None = None
    html_url: str | None = None
    stars: int = Field(default=0, alias="stargazers_count")
    forks: int = Field(default=0, alias="forks_count")
    watchers: int = Field(default=0, alias="watchers_count")
    open_issues: int = Field(default=0, alias="open_issues_count")
    language: str | None = None
    license_key: str | None = None
    license_name: str | None = None
    topics: list[str] = Field(default_factory=list)
    default_branch: str = "main"
    archived: bool = False
    fork: bool = False
    size: int = 0
