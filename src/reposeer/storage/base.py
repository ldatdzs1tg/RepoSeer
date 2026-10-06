"""Storage base classes and protocols."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BaseStorage(ABC):
    """Abstract interface for data storage handlers."""

    @abstractmethod
    def write(self, path: Path, data: Any) -> None:
        """Persist data to path."""
        pass

    @abstractmethod
    def read(self, path: Path) -> Any:
        """Load data from path."""
        pass
