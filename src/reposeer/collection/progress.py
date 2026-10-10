"""Consistent progress accounting and optional checkpoint persistence."""

from dataclasses import dataclass

from reposeer.collection.context import CollectionContext
from reposeer.collection.run_manager import RunManager


@dataclass
class CollectionProgress:
    """Track completed fetches across collectors in one resumable run context.

    These checkpoints describe fetch progress, not output storage transactions.
    Collectors do not automatically skip entities recorded here on resume.
    """

    context: CollectionContext
    run_manager: RunManager | None = None

    def record_success(self, key: str, value: str | int, count: int = 1) -> None:
        """Update a collector's last completed entity and persist the run if enabled."""
        if type(count) is not int or count < 0:
            raise ValueError("Success count must be a non-negative integer")
        self.context.set_checkpoint(key, value)
        self.context.record_success(count)
        self._save()

    def record_error(self) -> None:
        """Count a failure without advancing any collector checkpoint."""
        self.context.record_error()
        self._save()

    def _save(self) -> None:
        if self.run_manager is not None:
            self.run_manager.save_checkpoint(self.context)
