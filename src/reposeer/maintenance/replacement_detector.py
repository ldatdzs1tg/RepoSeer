"""Replacement repository detector."""
from __future__ import annotations

import re

from reposeer.schemas.project.maintenance_event import MaintenanceEvent


class ReplacementDetector:
    """Detects explicit official replacement / migration notices in authoritative docs or metadata."""

    REPLACEMENT_PATTERNS: list[str] = [
        r"replaced by\s+([A-Za-z0-9_./-]+)",
        r"superseded by\s+([A-Za-z0-9_./-]+)",
        r"moved to\s+([A-Za-z0-9_./-]+)",
        r"migrat(?:ed|e|ing)\s+to\s+([A-Za-z0-9_./-]+)",
        r"successor(?:\s+is)?\s+([A-Za-z0-9_./-]+)",
    ]

    def detect(self, repo_full_name: str, text: str) -> MaintenanceEvent | None:
        """Detect replacement notice in project docs or metadata text."""
        for pat in self.REPLACEMENT_PATTERNS:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                return MaintenanceEvent(
                    repo_full_name=repo_full_name,
                    event_type="replacement",
                    details=f"Matched replacement pattern '{pat}': {match.group(0)}",
                    confidence=0.9,
                )
        return None
