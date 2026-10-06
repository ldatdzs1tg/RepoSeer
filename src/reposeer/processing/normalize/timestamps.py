"""Timestamp parsing and normalization utilities."""

from datetime import UTC, datetime

from dateutil import parser


def normalize_datetime(dt_val: str | datetime | None) -> datetime | None:
    """Normalize any string or datetime representation to timezone-aware UTC datetime."""
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            return dt_val.replace(tzinfo=UTC)
        return dt_val.astimezone(UTC)
    try:
        parsed = parser.parse(dt_val)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return None
