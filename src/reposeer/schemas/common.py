"""Common schema base classes, mixins, and types."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return current timezone-aware UTC datetime."""
    return datetime.now(UTC)


class BaseSchema(BaseModel):
    """Base model for all RepoSeer schemas with standard configuration."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="ignore",
    )


class TimestampMixin(BaseModel):
    """Mixin adding created_at and updated_at tracking."""

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None


class ProvenanceMixin(BaseModel):
    """Provenance tracking for data lineage."""

    source: str
    run_id: str | None = None
    ingested_at: datetime = Field(default_factory=utc_now)

    def compute_hash(self, content: dict[str, Any] | str) -> str:
        """Calculate SHA256 fingerprint for idempotency & deduplication."""
        if isinstance(content, dict):
            raw = json.dumps(content, sort_keys=True, default=str)
        else:
            raw = str(content)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
