"""Collection error event schema."""

from datetime import datetime

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class CollectionErrorRecord(BaseSchema):
    """Failure record preserved for debugging and observability."""

    run_id: str
    target: str
    error_type: str
    error_message: str
    occurred_at: datetime = Field(default_factory=utc_now)
