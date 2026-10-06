# RepoSeer: Repository & Open-Source Ecosystem Intelligence

RepoSeer is an end-to-end data pipeline and intelligence system engineered to evaluate open-source software repository health, technology adoption, and external community trends.

## Tech Stack (Sprint 2)

- **Language & Runtime**: Python 3.12 managed via `uv`
- **Data Contracts**: Pydantic v2
- **Config Management**: `pydantic-settings` & PyYAML
- **HTTP Client**: HTTPX with Tenacity exponential backoff retries & rate limiting
- **Upstream Data**: GitHub REST API, PyPI, Google deps.dev, OSV
- **Web Extraction**: Trafilatura
- **Data Processing**: Polars
- **Storage Layer**:
  - Raw: JSONL (`data/raw/`)
  - Processed: Parquet (`data/processed/`)
  - Query / Analytics: Embedded DuckDB
- **CLI**: Typer + Rich
- **Code Quality & Testing**: Ruff, Pytest, respx

## Directory Layout

```text
reposeer/
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example
├── configs/            # YAML runtime configurations
├── docs/               # Architecture, contracts, and ADRs
├── src/reposeer/
│   ├── cli/            # Typer CLI commands
│   ├── clients/        # Upstream API clients (GitHub, PyPI, etc.)
│   ├── collection/     # Shared HTTP, retry, rate limit, checkpoints
│   ├── collectors/     # Domain data collectors
│   ├── technology/     # Rule-based detector & alias canonicalization
│   ├── processing/     # Polars normalization, deduplication, filtering
│   ├── storage/        # JSONL, Parquet, and DuckDB layers
│   ├── profiling/      # Dataset quality & profiling reports
│   └── pipelines/      # Integrated collection pipelines
├── data/               # Raw, processed, features, and reports
├── scripts/            # Utility scripts
└── tests/              # Pytest unit and integration test suites
```

## Quick Start

### 1. Prerequisites & Environment Setup

RepoSeer uses `uv` for fast, reproducible dependency resolution:

```bash
# Create Python 3.12 virtual environment
uv venv --python 3.12

# Activate virtual environment
source .venv/bin/activate

# Install package in editable mode with development dependencies
uv pip install -e ".[dev]"
```

### 2. Environment Variables

Copy the example `.env` file and set your GitHub token:

```bash
cp .env.example .env
# Edit .env and supply GITHUB_TOKEN
```

### 3. Running the CLI

```bash
# Display help
reposeer --help

# Collect repository metadata & activity
reposeer collect pallets/flask

# Normalize & process raw data into Parquet
reposeer process

# Generate data quality profiling report
reposeer profile

# Build feature tables
reposeer build-features
```

### 4. Running Tests & Quality Checks

```bash
# Run test suite
pytest

# Run linter
ruff check .

# Format code
ruff format .
```
