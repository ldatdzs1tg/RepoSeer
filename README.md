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

# Identify technology entities & generate external search terms (T-011)
reposeer detect-tech pallets/flask

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


## GitHub repository batch collection (T-013)

Collect several current GitHub repository observations with the same service/client:

```bash
reposeer collect --repos-file repositories.txt --source github
# The existing single-repository command remains available:
reposeer collect pallets/flask
```

The UTF-8 input file contains one `owner/repo` or HTTPS GitHub repository URL per line:

```text
# Current repository metadata targets
pallets/flask
https://github.com/pydantic/pydantic
psf/requests
```

Blank lines and full-line comments are ignored; inline comments are not parsed.
Supply either a positional target or `--repos-file`, with 1 to 1,000 batch entries.
Batch mode accepts `--source github` or `all`; it collects only GitHub metadata.
URLs with credentials, ports, queries, fragments, clone suffixes or extra path segments
are rejected. Invalid entries are reported without echoing potentially sensitive input.

`RepositoryService.collect_many(targets)` returns an immutable `RepositoryBatchResult`
with per-target items and actual raw/processed write counts. It shares one UUID run ID
across single-subject canonical requests. Equivalent slugs/URLs/case variants are
fetched once; verified duplicate numeric repository IDs are stored once per batch.
A new run appends new observations rather than deduplicating history.

Raw observations append to `data/raw/github/repositories.jsonl`. Eligible canonical
repositories append to `data/processed/repositories/repositories.parquet` via one
Parquet read/write at the end of the batch. Paths use configured `StoragePaths`.
Single-repository and batch collection share public verification and eligibility
screening. Private/internal repositories, unverified public status and invalid native
visibility metadata are blocked before raw persistence. Public forks, templates and
mirrors retain raw audit evidence but are excluded from processed output; single-repo
collection raises `CollectionError` with the screening reasons. Archived repositories
remain eligible; stars, popularity and primary language are not exclusion criteria.
Raw remains append-only. **Metadata screening passed does not mean that
package-to-repository mapping has been verified.** No abandonment labels are assigned.

Parquet writes use a temporary file in the destination directory, close its handle,
write the complete file with `zstd`, then replace the destination with `os.replace`.
Temporary files are cleaned up on failure; the previous destination remains intact
if writing or replacement fails. Atomic replacement depends on filesystem support
and does not resolve concurrent writers or cloud synchronization conflicts, or provide
full transaction semantics.

`complete` means all entries are stored, excluded or duplicate (exit 0). `partial`
means failures alongside stored/excluded entries; `failed` means there were failures
without a stored/excluded entry (both exit 1). A normalization failure preserves raw;
a final Parquet write failure raises `CollectionError` and does not report success.
Existing incompatible Parquet schemas require explicit migration. Alias/rename history
is not verified: a conflicting provider name is rejected even when its ID is familiar.
These are current observations, with exact raw retrieval provenance, not historical
backfill. PyPI mapping, historical snapshots and activity collection remain later tasks.

Run offline verification yourself in the existing Python 3.12 environment:

```bash
python -m pytest -q tests/integration/test_github_repository_batch.py tests/integration/test_github_contract_pipeline.py tests/contracts
python -m pytest -q -m "not smoke"
```

For manual live verification, create `repositories.txt` with public repositories you
intend to collect, configure `GITHUB_TOKEN` through the existing environment setup,
and run the batch command above. Check its summary, replay the raw JSONL using
`JsonlStorage.read_contract_records`, and inspect the projected Parquet with Polars
or DuckDB. Live collection writes new observations to the configured data directory.


## Technology entity detection & search terms (T-011)

Identifies technology entities, aliases, and generates multi-intent external search queries from repository metadata, READMEs, topics, dependency manifests (`requirements.txt`, `pyproject.toml`), or pinned signals:

```python
from reposeer.technology import detect_technologies

result = detect_technologies(
    repo="pallets/flask",
    manifest_files={"requirements.txt": "Werkzeug>=3.0\nJinja2>=3.1.2\nclick>=8.1.3\npytest"},
    topics=["python", "web", "framework", "flask"],
    readme_content="# Flask\nA micro web framework written in Python.",
)

# Output canonical entities conforming strictly to T-011 TechnologyEntity schema
for entity in result.entities:
    print(entity.technology_id, entity.name, entity.category)

# Output prioritized search terms for external news, maintenance, and security
for term in result.search_terms:
    print(f"[{term.priority}] ({term.platform}) {term.intent}: {term.query}")
```

CLI usage:

```bash
# Formatted table view
reposeer detect-tech pallets/flask

# JSON output according to schema T-011
reposeer detect-tech pydantic/pydantic --json
```
