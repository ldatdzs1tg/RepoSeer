# Data Architecture

## Storage Tiering (T-008 & T-019)

RepoSeer enforces a clear separation of data across storage tiers:

1. **Raw Storage Layer (`data/raw/`)**:
   - Format: JSON Lines (`.jsonl`)
   - Purpose: Store verbatim payloads returned by upstream APIs.
   - Benefits: Preserves provenance, append-only, debuggable, allows replay without API re-fetching.

2. **Processed Storage Layer (`data/processed/`)**:
   - Format: Apache Parquet (`.parquet`)
   - Purpose: Normalized, typed, deduplicated tables aligned with Pydantic contracts.
   - Processing Engine: Polars.
   - Compression: Zstandard / Snappy.

3. **Analytics & Query Layer**:
   - Engine: Embedded DuckDB.
   - Purpose: Direct zero-copy SQL analytics, EDA, feature calculation, and reporting directly over Parquet files.
