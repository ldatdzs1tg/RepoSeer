"""Resumed maintenance detector."""
from __future__ import annotations

import re

from reposeer.schemas.project.maintenance_event import MaintenanceEvent


class ResumedDetector:
    """Detects official revocation of deprecation/archival or resumed maintenance."""

    RESUMED_PATTERNS: list[str] = [
        r"no longer deprecated",
        r"maintenance resumed",
        r"unarchived",
        r"reactivated",
        r"actively maintained again",
        r"back under active maintenance",
        r"resuming maintenance",
    ]

    def detect(self, repo_full_name: str, text: str) -> MaintenanceEvent | None:
        """Detect resumed maintenance notice in text."""
        for pat in self.RESUMED_PATTERNS:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                return MaintenanceEvent(
                    repo_full_name=repo_full_name,
                    event_type="resumed",
                    details=f"Matched resumed pattern '{pat}': {match.group(0)}",
                    confidence=0.85,
                )
        return None

    def evaluate(self, repo: object) -> list[MaintenanceEvent]:
        """Detect unarchived repository state."""
        events: list[MaintenanceEvent] = []
        is_archived = getattr(repo, "archived", None)
        was_archived = getattr(repo, "was_archived", False)
        if is_archived is False and was_archived:
            events.append(
                MaintenanceEvent(
                    repo_full_name=getattr(repo, "full_name", ""),
                    event_type="resumed",
                    details="Repository unarchived on GitHub",
                    confidence=1.0,
                )
            )
        return events
