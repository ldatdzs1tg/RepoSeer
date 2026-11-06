"""Share original archive proofs across D09 batches without trusting grouping hints."""

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from tools.coordination._common import atomic_bytes

from reposeer.collection.persistence import write_json_atomic


def names_digest(names) -> str:
    return hashlib.sha256(json.dumps(sorted(names), separators=(",", ":")).encode()).hexdigest()


def covers_names(source: dict, names: set[str]) -> bool:
    if source.get("scan_names_sha256") == names_digest(names):
        return True
    return names.issubset(source.get("names", ()))


def saved_proofs(directory: Path):
    for path in sorted((directory / "archives").glob("*.json")):
        try:
            yield directory, json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue


def local_proof(directory: Path, source: dict, events: list[bytes], names: set[str]):
    """Keep the original matching lines in the batch so its receipt remains portable."""
    lines = [line for line in events if json.loads(line)["repo"]["name"].casefold() in names]
    body = b"".join(line if line.endswith(b"\n") else line + b"\n" for line in lines)
    digest = hashlib.sha256(body).hexdigest()
    revision = source["revision"]
    key = f"{revision['name']}.{revision['generation']}"
    relative = f"archives/{key}-{digest}.jsonl"
    atomic_bytes(directory / relative, body, immutable=False)
    # The copied subset supports positive matches only. Do not claim it contains
    # every name that the shared scanner checked in the original object.
    receipt = {
        key: value
        for key, value in source.items()
        if key not in {"names", "scan_names_sha256", "matched_names", "path", "content_sha256"}
    }
    receipt.update(
        path=relative,
        content_sha256=digest,
        names=sorted({json.loads(line)["repo"]["name"].casefold() for line in lines}),
    )
    write_json_atomic(directory / "archives" / f"{key}.json", receipt)
    return receipt, lines


class ArchivePool:
    """Index retained proofs once per pass; raw hashes are checked before reuse."""

    def __init__(self, output: Path, names: set[str]):
        self.directory = output / "archive-cache"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.names = names.copy()
        self.by_name = defaultdict(set)
        self.receipts = {}
        directories = [self.directory, *sorted(output.glob("batches/*/verification"))]
        for directory in directories:
            for _, source in saved_proofs(directory):
                try:
                    self.remember(directory, source)
                except (KeyError, OSError, TypeError, ValueError):
                    # Invalid receipts cannot hide a new verification attempt.
                    continue

    def remember(self, directory: Path, source: dict) -> None:
        revision = source["revision"]
        key = (directory, revision["name"], revision["generation"])
        self.receipts[key] = source
        for name in source.get("matched_names", source.get("names", ())):
            self.by_name[name].add(key)

    def proofs(self, names: set[str]):
        keys = set().union(*(self.by_name.get(name, set()) for name in names))
        for key in sorted(keys):
            yield key[0], self.receipts[key]
