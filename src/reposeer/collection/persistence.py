"""Small, atomic JSON persistence shared by caches and run checkpoints."""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def replace_atomic(source: Path, destination: Path) -> None:
    """Keep atomic replacement; briefly retry Windows sharing/access locks, then fail."""
    for attempt in range(4):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 3:
                raise
            time.sleep(0.025 * (attempt + 1))


def write_json_atomic(path: Path, data: Any) -> None:
    """Replace a JSON file only after its complete contents have reached disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(data, temporary_file, ensure_ascii=False, indent=2, allow_nan=False)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        replace_atomic(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
