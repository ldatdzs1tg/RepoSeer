#!/usr/bin/env python3
"""Script to rebuild processed Parquet tables from raw JSONL storage."""

import logging

from reposeer.storage.paths import paths
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.raw.jsonl import JsonlStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rebuild_processed")


def main() -> None:
    jsonl = JsonlStorage()
    parquet = ParquetStorage()

    raw_file = paths.raw_path("github", "repositories")
    if raw_file.exists():
        records = jsonl.read(raw_file)
        target = paths.processed_path("repositories")
        parquet.write(target, records)
        logger.info("Rebuilt %d records into %s", len(records), target)
    else:
        logger.info("No raw repositories found at %s", raw_file)


if __name__ == "__main__":
    main()
