"""Association between a repository and a detected technology."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class RepoTechnologyAssociation(BaseSchema):
    """Normalized technology usage detected within a repository."""

    repo_full_name: str
    name: str  # Canonical technology name
    source: str  # e.g. "requirements.txt", "package.json", "Dockerfile"
    confidence: float = 1.0
    detected_at: datetime = Field(default_factory=utc_now)
    version_hint: str | None = None
