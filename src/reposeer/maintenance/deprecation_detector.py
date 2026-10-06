"""Deprecation notice detector in README/descriptions."""

import re

from reposeer.schemas.project.maintenance_event import MaintenanceEvent


class DeprecationDetector:
    DEPRECATION_PATTERNS = [
        r"deprecated",
        r"no longer maintained",
        r"moved to",
        r"archived",
        r"unmaintained",
    ]

    def detect(self, repo_full_name: str, text: str) -> MaintenanceEvent | None:
        for pat in self.DEPRECATION_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                return MaintenanceEvent(
                    repo_full_name=repo_full_name,
                    event_type="deprecated",
                    description=f"Matched deprecation pattern '{pat}'",
                    confidence=0.8,
                )
        return None
