"""Data lineage and provenance tracking helpers."""

import uuid
from datetime import UTC, datetime
from typing import Any


def create_provenance_record(
    source: str,
    entity_id: str,
    run_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate standardized lineage metadata."""
    return {
        "source": source,
        "entity_id": entity_id,
        "run_id": run_id or str(uuid.uuid4()),
        "ingested_at": datetime.now(UTC).isoformat(),
        **(extra or {}),
    }
