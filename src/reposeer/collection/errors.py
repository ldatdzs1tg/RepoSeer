"""Collection error tracking and handling utilities."""

import logging

from reposeer.collection.progress import CollectionProgress

logger = logging.getLogger("reposeer.collection")


def handle_collection_error(
    error: Exception, entity_id: str, *, progress: CollectionProgress | None = None
) -> None:
    """Log and track collector errors consistently."""
    if progress is not None:
        progress.record_error()
    logger.error(
        "Failed to collect data for '%s': %s",
        entity_id,
        str(error),
        extra={"entity_id": entity_id, "run_id": progress.context.run_id if progress else None},
    )
