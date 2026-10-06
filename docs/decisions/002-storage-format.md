# ADR 002: Storage Formats

We use JSONL for raw captures and Parquet for processed tables. Parquet provides columnar compression, typed columns, and direct querying with DuckDB.
