"""Read-only validation of canonical raw envelopes and T-017 capture payloads."""

import argparse
from collections import Counter
from pathlib import Path

from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.contracts import validate_record
from reposeer.storage.raw.jsonl import JsonlStorage


def validate_file(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise ValueError(f"Capture file does not exist: {path}")
    counts = Counter()
    for line, value in enumerate(JsonlStorage().iter_records(path), 1):
        try:
            raw = validate_external_document_record(validate_record(value))
        except ValueError as error:
            # Validation diagnostics may echo source text; expose only the location.
            raise ValueError(f"Invalid external capture at {path}, record {line}") from error
        counts[raw.payload["content_status"]] += 1
    if not counts:
        raise ValueError(f"No external captures to validate: {path}")
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+", help="External documents JSONL files.")
    args = parser.parse_args()
    for path in args.paths:
        try:
            counts = validate_file(path)
        except (ValueError, OSError) as error:
            parser.exit(1, f"{error}\n")
        print(f"Validated {sum(counts.values())} captures in {path}: {counts}")


if __name__ == "__main__":
    main()
