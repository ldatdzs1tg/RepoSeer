"""Project maintenance event contract."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class MaintenanceEvent(BaseSchema):
    """Signal indicating maintenance state transition."""

    repo_full_name: str
    event_type: str  # "archived", "deprecated", "resumed", "replacement"
    event_date: datetime | None = None
    confidence: float = 1.0
    detected_at: datetime = Field(default_factory=utc_now)
    details: str | None = None
