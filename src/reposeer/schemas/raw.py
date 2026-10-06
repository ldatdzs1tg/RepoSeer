"""Raw ingestion schemas preserving unaltered payloads from source APIs."""

from datetime import datetime
from typing import Any

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now


class RawRecord(BaseSchema):
    """Container for verbatim raw API payload alongside collection metadata."""

    source: str
    entity_id: str
    endpoint: str | None = None
    payload: dict[str, Any] | list[Any]
    collected_at: datetime = Field(default_factory=utc_now)
    run_id: str | None = None
    status_code: int = 200
    headers: dict[str, str] = Field(default_factory=dict)
