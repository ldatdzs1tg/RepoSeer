# Application orchestrator (T-027)

`reposeer.analysis.application.ApplicationOrchestrator.analyze_repository(identifier, **options)`
returns a validated T-021 `FinalReport`. `reposeer analyze-repository` exposes the same
boundary. The earlier `reposeer analyze --feature-package ... --model ...` command remains
available for callers that already have a CORE feature package.

## Python and UI interface

```python
from pathlib import Path
from reposeer.analysis.application import ApplicationOrchestrator
from reposeer.analysis.application_inputs import StoredAnalysisBackend

backend = StoredAnalysisBackend.from_files([
    Path("examples/application/synthetic_bundle.json"),
])
orchestrator = ApplicationOrchestrator(
    backend,
    model_path=Path("examples/application/synthetic_model_a.json"),
    reports_dir=Path("data/reports/analysis"),
)
report = orchestrator.analyze_repository(
    "synthetic-demo",
    cutoff_at="2026-10-01T00:00:00.000Z",
    purpose="retrospective_analysis",
    mode_requested="ENHANCED",
    include_external_insights=True,
    include_dependency_impact=True,
)
payload = report.model_dump(mode="json")
```

Options are validated by `AnalyzeRequest`: PyPI name/ID or GitHub URL, optional
`package_name`, exact `cutoff_at`, `mode_requested`, `purpose`,
`include_external_insights`, `include_agents`, and `include_dependency_impact`.
The online interface accepts `current_inference` and `retrospective_analysis`;
prospective backtest records remain exclusive to D10. Future cutoffs are rejected.

The convenience `analyze_repository(identifier, model_path=..., data_dir=...)` reads
only `data_dir/analysis_inputs/*.json` and saves reports under `data_dir/reports/analysis/`.
Advanced callers construct `ApplicationOrchestrator` to inject independently trusted
ledger, verification, model and provider dependencies. Importing it creates no clients.

## Offline demo

```bash
uv sync --frozen --extra dev --extra functional
uv run --frozen reposeer analyze-repository synthetic-demo \
  --input-bundle examples/application/synthetic_bundle.json \
  --model examples/application/synthetic_model_a.json \
  --mode ENHANCED --purpose retrospective_analysis --insights --agents \
  --agent-replay examples/application/synthetic_agent_replay.json --impact \
  --output data/reports/application-demo.json
```

All inputs, source identities and model parameters in this example are synthetic.
The released flag is a test fixture, not an approval for a production estimator.
The zero-weight fixture produces probability `0.5`; this demonstrates contract wiring,
not measured predictive quality. No network request, training or live LLM call occurs.
The result uses CORE with `model_b_unavailable`, adds cited external and agent context,
and marks dependency impact unavailable. Agent findings remain report-only, including
the sample future end-of-support claim. The CLI prints only FinalReport JSON to stdout.

Repeat `--input-bundle` for multiple independently reviewed package/cutoff bundles.
A GitHub URL resolves only through the retained PyPI/GitHub mapping observations,
their exact observation identity/time and payload/body hashes in mapping lineage.
It is never resolved by equating the repository name with a PyPI name. If multiple
packages share a repository, pass `--package <name>`. If a specific cutoff has no
bundle, the result is no-score; the service does not substitute another cutoff.

Without `--output`, each result is saved atomically as
`data_dir/reports/analysis/<analysis_run_id>.json`. An explicit output is created
exclusively: an existing file is not replaced. Invalid configuration or an unresolved
GitHub package selection exits 2. A structured `failed` report exits 1; valid known-status,
predicted, fallback and insufficient-evidence reports exit 0. A package identity must
be established before a FinalReport can exist; `AnalysisInputError` carries safe
selection/input reason codes for that earlier boundary.

## Explicit live collection

```bash
uv run --frozen reposeer analyze-repository flask --live \
  --purpose current_inference --model /path/to/frozen-model-a.json \
  --data-dir data --insights
```

`CollectionAnalysisBackend` calls the existing T-020 `CollectionPipeline`, followed by
T-022 `FeatureService.build_from_processed` with an explicit package/repository target.
It preserves their raw IDs, run manifests and processed tables. Workspaces are isolated
at `data_dir/analysis_runs/<analysis_run_id>/`; feature snapshots are persisted there,
and `metadata/analysis_bundle.json` retains the derived application input. This saved
bundle describes observations; it does not grant an independent historical review.

Without an explicit cutoff, live acquisition fixes the cutoff after collection. An
explicit past cutoff remains unchanged. Mapping validity/availability starts at actual
observation time, and current repository-only mapping never becomes package-subdirectory
scope. The adapter does not promote list pagination, current GitHub activity or GitHub
Releases into complete historical package coverage or PyPI release history. A run with
no usable historical CORE features returns no-score. Provider failures, unsupported
repositories and unresolved mappings have different structured outcomes.

CORE without requested insights defers external providers. ENHANCED only acquires
external data automatically when a released Model B registration is present; remaining
compatibility checks run against the actual CORE package. Default acquisition budgets
are two GitHub pages/200 records, one discovery page/30 records and 12 crawl candidates,
using the existing bounded HTTP clients and source failure behavior.

## Status, model and evidence ordering

1. Resolve the package and reject invalid/ambiguous input before acquisition.
2. Consult the reviewed CORE official ledger and any injected `OfficialStatusProvider`.
   An as-of known cessation or future plan returns before collection/model loading.
3. Acquire/build CORE and repeat status evaluation before frozen Model A inference.
4. Run requested T-025 context and T-026 specialists independently of the CORE score.
   Consult the independent official ledger again before selecting a final prediction.
5. Use a registered, released, compatible Model B only with complete independently
   verified T-025 census/processing coverage. Otherwise retain CORE with a fallback reason.
6. Attach independently sourced D12 impact if requested, validate and persist FinalReport.

Known-status reports remove both prediction fields and clear model selection/fallback.
An unavailable official ledger fails closed rather than returning a score with an
unchecked status. Specialist claims never enter that ledger automatically.

`EnhancedModel` is an injected trusted registry interface with a frozen `ModelManifest`,
`release_eligible`, `compatible(core)` and `predict(core, external)`. There is no built-in
Model B training or fabricated enhanced estimator. Its returned manifest must equal
the registration; T-021 rechecks source coverage, query hashes, schema, timestamps and
E1/E2 lineage. E2 additionally requires trusted T-025 event/processing verification.
A successful ENHANCED report retains the original `core_prediction` separately.
Model A/B probabilities are never blended or adjusted by narrative insights.

The ledger, archive/census verifier, optional D06 receipt backend, calibrated Model B
registration and dependency corpus are deployment dependencies. No JSON boolean or
agent recommendation replaces them. The local bundle adapter consumes an upstream
reviewed `CoreFeaturePackage`; structural validation does not authenticate its history.

## Specialist and impact handoff

Inject `agent_coordinator` or a lazy `agent_factory`. The CLI requires `--agents` plus
either `--agent-replay <file>` or `--agent-live` to select a provider. Live provider
configuration is documented in [the T-026 guide](../../src/reposeer/agents/README.md).
Missing configuration or provider failure records an optional-stage failure while
preserving valid CORE results. Provider clients created by the CLI are closed there.

An `AnalysisBundle` may carry prepared T-024 requests and exact `agent_sources`.
Otherwise relevant T-025 documents produce bounded research requests for the same three
specialists. Truncation/omission counters remain in the D07 input audit. Run identity
is assigned by the application, while specialist request IDs remain replayable.
Returned identities, original request/response pairs, source revisions, content hashes,
quotes and offsets are rechecked before projection to T-021 evidence. Research findings
need a retained full document or a bound D06 passage to establish exact source offsets.
Unresolved findings are omitted with an explicit limitation; no source is invented.

All projected D07 findings use `report_only`. Processing assignments do not become a
verified unique-document census or a D08 denominator. FinalReport agent coverage keeps
historical validity unverified and eligible/processed document denominators null.
Independently validated T-025 E2 inputs remain a separate path to a registered Model B.

`impact_provider(package_id, cutoff)` returns `ImpactContext`, or a reviewed bundle may
supply an independent impact result and provenance. T-021 checks graph time, corpus
counts and source lineage. Missing/failed/invalid impact becomes `unavailable`, never
zero dependents, and cannot alter an abandonment prediction.

## Verification

```bash
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q tests/integration/test_app_orchestrator.py \
  tests/integration/test_app_collection_backend.py tests/integration/test_analyze_cli.py
```

Tests use explicit synthetic models, ledger/verifier doubles, recorded specialist
drafts and HTTPX MockTransport with the real collection/feature pipelines. They check
status precedence, historical mapping failure, model release/coverage gates, E1/E2
fallback and success, exact evidence lineage, provider failures, isolated workspaces,
immutable model/input artifacts, CLI behavior and report persistence. They do not claim
live API availability, production model quality or independent archive verification.
