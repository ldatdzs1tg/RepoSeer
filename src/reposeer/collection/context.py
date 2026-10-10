"""Execution context for collection runs."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class CollectionContext:
    """Contextual metadata passed across collectors during execution."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    checkpoint_state: dict[str, str | int] = field(default_factory=dict)
    collected_count: int = 0
    error_count: int = 0

    def record_success(self, count: int = 1) -> None:
        """Increment collected items count."""
        if type(count) is not int or count < 0:
            raise ValueError("Success count must be a non-negative integer")
        self.collected_count += count

    def record_error(self, count: int = 1) -> None:
        """Increment error count."""
        if type(count) is not int or count < 0:
            raise ValueError("Error count must be a non-negative integer")
        self.error_count += count

    def set_checkpoint(self, key: str, value: str | int) -> None:
        """Record cursor or checkpoint identifier for resuming."""
        if not isinstance(key, str) or not key or type(value) not in (str, int):
            raise ValueError(
                "Checkpoint keys must be non-empty strings and values strings or integers"
            )
        self.checkpoint_state[key] = value

    def get_checkpoint(self, key: str, default: str | int | None = None) -> str | int | None:
        """Retrieve checkpoint state."""
        return self.checkpoint_state.get(key, default)
