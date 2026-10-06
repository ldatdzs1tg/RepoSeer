"""Maintenance signal detector."""

from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.schemas.project.maintenance_event import MaintenanceEvent


class MaintenanceDetector:
    """Analyzes repository signals to determine maintenance status."""

    def evaluate(self, repo: RepositoryRecord) -> list[MaintenanceEvent]:
        events = []
        if repo.archived:
            events.append(
                MaintenanceEvent(
                    repo_full_name=repo.full_name,
                    event_type="archived",
                    description="Repository marked as archived on GitHub",
                    confidence=1.0,
                )
            )
        return events
