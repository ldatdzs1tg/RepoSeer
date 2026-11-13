"""Maintenance signal detector."""
from __future__ import annotations

from reposeer.maintenance.deprecation_detector import DeprecationDetector
from reposeer.maintenance.replacement_detector import ReplacementDetector
from reposeer.maintenance.resumed_detector import ResumedDetector
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.schemas.project.maintenance_event import MaintenanceEvent


class MaintenanceDetector:
    """Analyzes repository signals to determine maintenance status."""

    def __init__(self) -> None:
        self.deprecation_detector = DeprecationDetector()
        self.replacement_detector = ReplacementDetector()
        self.resumed_detector = ResumedDetector()

    def evaluate(self, repo: RepositoryRecord) -> list[MaintenanceEvent]:
        events = []
        if repo.archived:
            events.append(
                MaintenanceEvent(
                    repo_full_name=repo.full_name,
                    event_type="archived",
                    description="Repository marked archived on GitHub",
                    confidence=1.0,
                )
            )
        return events
