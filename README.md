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

### 5. Common Collector Utilities

GitHub and external collectors share HTTP timeout, retry/backoff, rate-limit
handling, optional TTL response caching, logging, and atomic run checkpoints.
See [Common collector utilities](docs/architecture/collector_utilities.md) for
configuration, shared-session usage, checkpoint semantics, and the offline smoke test.


## Data contract (T-011)

Canonical exchange contract `0.1.0` aligns with T-008 architecture v0.3. Models, identity helpers, strict record/pair validation and typed GitHub adapters live in `src/reposeer/contracts/`; `src/reposeer/schemas/` remains legacy/internal DTOs.

```python
from reposeer.contracts import RawRecord, GitHubRepository, validate_record
from reposeer.contracts.adapters import GitHubRepositoryContractCollector, GitHubRepositoryContractNormalizer
```

`RepositoryService.collect_and_store` now follows native GitHub JSON → validated RawRecord → append-only JSONL → validated GitHubRepository → flattened Parquet → DuckDB. It uses T-012 HTTP utilities and preserves injected session ownership. Unknown counts stay null; unavailable raw HTTP body hashes/status stay null. Current observations cannot become historical features. Raw persistence and fetch checkpoints are separate.

Generated artifacts: `docs/contracts/data_contract.schema.json`, `types.ts`, `schema_reference.md`. See [contract](docs/contracts/data_contract.md), [feature grain](docs/contracts/feature_grain.md), [raw](docs/contracts/raw_record.md) and [processed models](docs/contracts/processed_entities.md). `examples/contracts/*.json` contains 19 synthetic, cross-consistent fixtures; invalid examples are under `tests/fixtures/contracts/invalid/`.

Using Python 3.12 with the activated project virtualenv (on Windows: `.venv\Scripts\Activate.ps1`):

```bash
uv sync --extra dev --frozen
python scripts/export_contract.py
python scripts/export_contract.py --check
python scripts/validate_examples.py
python -m pytest -q tests/contracts tests/integration/test_github_contract_pipeline.py
python -m pytest -q -m "not smoke"
python -m pytest -q tests/unit/collection tests/integration/test_github_pipeline.py tests/integration/test_external_pipeline.py tests/integration/test_collector_utilities.py
python -m ruff check src/reposeer/contracts scripts/export_contract.py scripts/validate_examples.py tests/contracts tests/integration/test_github_contract_pipeline.py
```

The offline integration demo uses fake GitHub payloads and HTTPX MockTransport, replays JSONL without refetching, verifies nullable Parquet columns and queries them with DuckDB. Default verification needs no network/credentials; live `smoke` tests are optional. The repository CLI remains a local demo entrypoint. Other collectors, technology linking business logic, snapshot feature computation, broader package-first CLI, dependency/vulnerability and label pipelines are outside T-011. Stored legacy files are never silently migrated; see the explicit ID migration helper and dataset migration notes in the contract.
