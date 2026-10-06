"""Collection run tracking schema."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class CollectionRun(BaseSchema):
    """Metadata tracking an individual batch or pipeline execution."""

    run_id: str
    target: str
    status: str = "running"
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    records_collected: int = 0
    errors_count: int = 0
