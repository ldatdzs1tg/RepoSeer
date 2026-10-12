"""Raw storage handler for JSON Lines (JSONL)."""

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from reposeer.contracts import RawRecord, validate_record
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

    def append_contract_record(self, path: Path, record: RawRecord) -> None:
        """Validate and append one canonical raw envelope; never overwrite."""
        if not isinstance(record, RawRecord):
            raise TypeError("strict raw storage requires contracts.RawRecord")
        validated = validate_record(record)
        line = json.dumps(validated.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)

    def append_contract_records_durable(
        self, path: Path, records: Iterable[RawRecord]
    ) -> int:
        """Append one bounded, prevalidated step and sync it before returning."""
        lines = []
        for record in records:
            if not isinstance(record, RawRecord):
                raise TypeError("strict raw storage requires contracts.RawRecord")
            validated = validate_record(record)
            lines.append(json.dumps(
                validated.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                allow_nan=False, separators=(",", ":"),
            ) + "\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            for line in lines:
                stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        return len(lines)

    def read_contract_records(self, path: Path) -> list[RawRecord]:
        """Strict replay: every line must be a canonical raw JSON object."""
        if not path.exists():
            return []
        records = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                record = validate_record(line)
                if not isinstance(record, RawRecord):
                    raise ValueError("strict raw storage encountered a non-raw record")
                records.append(record)
        return records
