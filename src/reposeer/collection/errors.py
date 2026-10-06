"""Collection error tracking and handling utilities."""

import logging

logger = logging.getLogger("reposeer.collection")


def handle_collection_error(error: Exception, entity_id: str) -> None:
    """Log and track collector errors consistently."""
    logger.error("Failed to collect data for '%s': %s", entity_id, str(error), exc_info=True)
