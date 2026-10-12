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


## Canonical exchange and feature layer (T-008 v0.3 / T-011)

`src/reposeer/contracts/` is the single source of truth for exchange models at schema `0.1.0`; `src/reposeer/schemas/` remains legacy/internal DTOs. Raw strict JSONL preserves native provider payloads; normalized repositories use an explicit flat Parquet projection with provenance; feature snapshots use separate Parquet at `(package_id, cutoff_at)`; DuckDB queries Parquet. Repository grouping uses stable GitHub IDs, with verified package↔repository mapping. All exchange timestamps are UTC milliseconds and historical input must be cutoff-safe. See [data contract](../contracts/data_contract.md).
