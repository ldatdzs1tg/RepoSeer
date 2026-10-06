"""GitHub commit activity schema."""

from datetime import datetime

from reposeer.schemas.common import BaseSchema


class CommitRecord(BaseSchema):
    """Commit record capturing development activity and cadence."""

    sha: str
    repo_full_name: str
    author_name: str | None = None
    author_email: str | None = None
    committed_at: datetime
    message: str
