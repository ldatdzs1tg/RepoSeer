"""Raw storage handler for JSON Lines (JSONL)."""

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from reposeer.storage.base import BaseStorage


class JsonlStorage(BaseStorage):
    """Appendable JSON Lines storage for raw data capture."""

    def append_record(self, path: Path, record: dict[str, Any] | Any) -> None:
        """Append a single record as a JSON line."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump(mode="json") if hasattr(record, "model_dump") else record
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")

    def append_records(self, path: Path, records: Iterable[dict[str, Any] | Any]) -> int:
        """Append multiple records into the file. Returns total count written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(path, "a", encoding="utf-8") as f:
            for record in records:
                payload = (
                    record.model_dump(mode="json") if hasattr(record, "model_dump") else record
                )
                f.write(json.dumps(payload, default=str) + "\n")
                count += 1
        return count

    def write(self, path: Path, data: Iterable[dict[str, Any] | Any]) -> None:
        """Overwrite target file with records."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for record in data:
                payload = (
                    record.model_dump(mode="json") if hasattr(record, "model_dump") else record
                )
                f.write(json.dumps(payload, default=str) + "\n")

    def read(self, path: Path) -> list[dict[str, Any]]:
        """Read all lines into a list of dicts."""
        if not path.exists():
            return []
        records = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def iter_records(self, path: Path) -> Iterator[dict[str, Any]]:
        """Streaming iterator over lines in JSONL file."""
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)
