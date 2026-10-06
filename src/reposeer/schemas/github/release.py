"""GitHub release and version tagging schema."""

from datetime import datetime

from reposeer.schemas.common import BaseSchema


class ReleaseRecord(BaseSchema):
    """Release record representing artifact publication event."""

    tag_name: str
    name: str | None = None
    repo_full_name: str
    published_at: datetime | None = None
    is_prerelease: bool = False
    is_draft: bool = False
