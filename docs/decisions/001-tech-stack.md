# ADR 001: Technology Stack Selection

- **Python 3.12**: Unifies data collection, data engineering (Polars), and downstream LLM agents.
- **uv**: High performance package and virtualenv management with reproducible locks.
- **Pydantic v2**: High throughput schema validation and structured data contracts.
- **HTTPX + Tenacity**: Modern sync/async HTTP requests with resilient backoff retry.
- **Polars**: Memory-efficient tabular manipulation and transformations.
- **Parquet + DuckDB**: Columnar storage and serverless SQL query execution without database servers.
- **Typer**: Type-safe CLI orchestration.
