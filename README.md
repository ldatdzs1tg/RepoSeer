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

# Resolve technologies & generate external search terms (T-015)
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


## Technology resolution & search terms (T-015)

T-015 resolves curated technology concepts from supplied repository metadata (`GitHubRepository`,
legacy `RepositoryRecord`, dictionaries, or a recorded repository `RawRecord`), owner/repo strings,
README/description text, topics, Python manifests, explicit dependencies and pinned aliases/slugs or
`tech:<slug>` IDs. It performs no network requests, external search, LLM calls or storage writes.
An owner/repo string supplies a textual subject; it does not fetch current GitHub metadata.

`DetectionResult.resolved_specs` contains curated `TechSpec` catalog identities, `primary_spec` is the
explicitly pinned or exact curated upstream concept, and `detected_signals` explains each match.
Signals contain `technology_id`, optional numeric `repo_id`, textual `repo_full_name`, source,
confidence, matched token/context, dependency scope, source URL and recorded raw IDs when present.
Observation source, retrieval time and availability are retained separately from signal source.
Relationships distinguish `implements`, runtime `depends_on`, `optional_dependency`,
`dev_dependency`, language/topics, pins and text `mentions`. README examples, badges and install
commands remain mentions; they do not establish package usage. Numeric IDs in dictionaries and
legacy DTOs remain unverified until a matching GitHub raw observation validates both the ID and
repository name. Validated canonical `GitHubRepository` inputs also establish verified identity.
Signals and `metadata["repo_id"]` use only that verified ID; `metadata["supplied_repo_id"]` separately
retains a syntactically valid caller ID without claiming verification. Exact curated owner/repo
mapping resolves an upstream concept without proving a numeric ID. Unrelated repositories named
`requests` or `flask` are not upstream matches.
PyPI distribution names use the shared identity normalization and curated package mappings;
unknown dependencies are diagnostics, not new technology concepts.

```python
from reposeer.technology import detect_technologies

result = detect_technologies(
    repo="pallets/flask",
    manifest_files={"requirements.txt": "Werkzeug>=3.0\nJinja2>=3.1.2\nclick>=8.1.3"},
    topics=["python", "web"],
    readme_content="# Flask\nA micro web framework written in Python.",
)
for spec in result.resolved_specs:
    print(f"tech:{spec.slug}", spec.name, spec.category)
for term in result.search_terms:
    print(term.priority, term.platform, term.intent, term.query)
assert result.entities == []  # No recorded observation was supplied.
```

Canonical output uses only `reposeer.contracts.TechnologyEntity`, T-011 version `0.1.0`.
`entities` and `primary_technology` contain only validated canonical output backed by recorded
observations or existing validated canonical provenance. Without it, entities are omitted with
explicit diagnostics, while specs/signals/search terms remain usable by T-016. The resolver never
invents `raw:<UUID>`, official URLs, retrieval times or availability times. Unknown `available_at`
stays null. Provenance establishes observation origin, not technology identity by itself.
`TechSpec.to_contract_entity()` requires a matching catalog or GitHub repository `raw_record`;
arbitrary provenance alone and direct document construction are rejected. Documentation must go
through detector checks binding exact recorded content, a recognized signal and a verified subject.
The existing `provenance`, `raw_record_id`, `source_url` and timestamp keywords remain accepted as
consistency checks against actual evidence. A catalog observation proves a concept, not arbitrary
repository links; only matching recorded curated upstream evidence supports `repo_ids`.
Catalog specs store collection fields as immutable tuples while accepting existing list-based construction.
The detector's optional `clock` is retained for compatibility and never supplies evidence time.

To normalize an existing persisted repository observation, pass the actual raw record:

```python
from pathlib import Path
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology import detect_technologies

records = JsonlStorage().read_contract_records(Path("data/raw/github/repositories.jsonl"))
if records:
    result = detect_technologies(records[0])
    for entity in result.entities:
        print(entity.technology_id, entity.provenance.raw_record_ids)
```

`raw_records=` can additionally supply already-recorded evidence. A GitHub repository observation
supports only its exact payload metadata, not separately supplied README/manifests. For supplied
content, an existing `source_archive` or `project_docs` observation must have the same verified
`repo_id` and exact payload `readme_content`, `manifest_files` or `dependencies`; a supplied
`full_name` must agree. A `technology_catalog` observation identifies only its matching payload
`slug`, `id` or `technology_id` (canonical slug or `tech:<slug>`); identity fields and any
technology identity in `source_record_id` must agree. Existing canonical pinned entities preserve
their original provenance and links, as do valid historical canonical repository observations.
Callers are responsible for recording observations before passing them; this resolver does not
verify external storage persistence or build a raw writer. Canonical `repo_ids` are limited to
recorded curated upstream implementation evidence; dependencies and mentions remain structured
signals, without being converted into upstream identity links. A catalog-only observation does
not establish that a repository uses the technology.

Detection precedence is pins, exact curated upstream mapping, dependencies, topics, language,
then README heuristics. Stronger evidence determines confidence; all distinct signal meanings
remain visible. Results and queries have stable ordering and deduplication. Search priority 1
is repository-specific discovery, 2 primary technology, 3 its ecosystem and 4 secondary integration.
`SearchTermGenerator(max_terms_per_intent=2)` limits each `(intent, platform)` across the entire
call after priority sorting, rather than granting every dependency its own budget. Zero disables
queries. Secondary concepts get ecosystem/integration queries only. Queries use contextual
keywords without provider-specific Boolean operators or ambiguous aliases; ecosystem topics
already covered by the primary name/category are omitted case-insensitively, while useful phrases
such as `HTTP client` remain. With no primary,
verified subject-only queries have `SearchTerm.tech_slug=None`; an unverified unknown subject
produces no invented canonical slug or repository queries. This nullable field is an explicit
resolver API refinement and does not change T-011.

CLI usage (local/manual input; no external retrieval):

```bash
reposeer detect-tech . --pinned tech:flask
reposeer detect-tech pallets/flask --readme README.md --requirements requirements.txt --topic python
reposeer detect-tech pydantic/pydantic --pyproject pyproject.toml --pinned tech:pydantic --json
```

The CLI preserves its existing options. Local directories auto-read supported README files,
`requirements.txt`, `pyproject.toml`, `setup.cfg` and `setup.py`; explicit file options override
local files. File errors are Typer errors, parser diagnostics and missing evidence are displayed,
and JSON includes input mode and `externally_retrieved=False`. JSON is a structured resolver
result; only its `entities` follow the canonical T-011 entity contract.

Python manifests use `packaging.Requirement` (the existing locked packaging distribution is now
a runtime dependency) for PEP 508 names, extras, markers and named URLs. URL fragments survive
comment stripping; named VCS `#egg=` references are supported. Pip options/includes/constraints
are never dependencies; referenced files are not read implicitly. `tomllib` handles PEP 621,
optional groups, Poetry groups and explicit Flit requirements. Build-system dependencies and
project names are excluded. Scopes distinguish runtime, optional and dev declarations. Invalid
TOML and unsupported forms produce diagnostics via `parse_manifests()`; compatibility name-list
parsers raise for parse/unsupported errors rather than silently returning an empty success.
`setup.cfg` is parsed statically and `setup.py` only accepts literal top-level setuptools arguments;
no project code is executed. Dynamic dependency calculation and full dependency resolution are
unsupported. README heuristics suppress common English words, negative/comparative and historical
mentions, but are not a semantic parser and can still miss context. Confidence is recognition
confidence, never abandonment probability, and README keywords do not change technology lifecycle.

**T-015 does not establish historical cutoff-safe technology usage merely by observing current
project files.** T-016 providers, T-017 crawling, T-018 normalization/storage pipelines, T-019
processed storage, live auto-fetch and historical reconstruction remain outside this resolver.

Manual verification in the existing Python 3.12 environment:

```bash
python -m pytest -q tests/unit/technology tests/integration/test_technology_detection.py
python -m pytest -q tests/contracts
python -m pytest -q -m "not smoke"
python -m ruff check src/reposeer/technology src/reposeer/cli/detect_tech.py tests/unit/technology tests/integration/test_technology_detection.py
reposeer detect-tech . --pinned tech:flask --json
```
