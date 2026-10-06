"""In-repo project documentation contract."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class ProjectDocument(BaseSchema):
    """Repository artifact such as README, CHANGELOG, or ARCHITECTURE."""

    repo_full_name: str
    doc_type: str  # "readme", "changelog", "contributing", "license"
    content: str
    format: str = "markdown"
    collected_at: datetime = Field(default_factory=utc_now)
