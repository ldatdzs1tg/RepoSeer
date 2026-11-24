# Demo HTTP API (T-028)

The replaceable `frontend/` client and Python backend run in separate processes.
The server in `reposeer.demo.api` invokes `ApplicationOrchestrator` (T-027) and
validates every returned T-021 FinalReport before exposing it. No frontend assets
are served by the API, and no backend implementation is imported by the UI.
See [frontend setup](../../frontend/README.md) for a runnable two-terminal example.

## HTTP contract, version 1

Base URL: `http://127.0.0.1:8765`. All response bodies are JSON. Requests to POST
endpoints must use `Content-Type: application/json`. Browser access requires the
exact server-configured origin (default `http://localhost:5173`).

| Method | Path | Response |
| --- | --- | --- |
| GET | `/api/health` | `200 {"status":"ok","api_version":"1"}` |
| GET | `/api/scenarios` | `200 {"scenarios":[{"id":"core","label":"CORE prediction"},…]}`; empty when mocks disabled |
| POST | `/api/analyses` | `202 {"id":"<uuid>","status":"queued","source":"orchestrator"}` |
| GET | `/api/analyses/<uuid>` | `200` job object or `404 {"error":"job_not_found"}` |
| OPTIONS | API paths | `204` preflight for the permitted origin; otherwise `403` |

Submit an orchestrator request:

```json
{
  "source": "orchestrator",
  "request": {
    "identifier": "synthetic-demo",
    "package_name": null,
    "cutoff_at": null,
    "mode_requested": "ENHANCED",
    "purpose": "retrospective_analysis",
    "include_external_insights": true,
    "include_agents": true,
    "include_dependency_impact": true
  }
}
```

`request` follows `reposeer.analysis.application_inputs.AnalyzeRequest`: identifier
is required; the other fields above have defaults. Modes are CORE/ENHANCED;
purposes are current_inference/retrospective_analysis. UTC cutoffs use ISO 8601
with `Z`. PyPI names/IDs and verified GitHub URLs are accepted by the existing
input boundary. Unknown fields and coerced boolean strings are rejected.

For a synthetic report state, submit exactly:

```json
{"source":"mock","scenario":"scheduled"}
```

Use an ID returned by `/api/scenarios`: core, enhanced, fallback, abandoned,
scheduled or no-score. The mock directory is chosen by the server operator;
HTTP requests cannot choose file paths. Mock and orchestrator request fields
cannot be combined. Mock fixtures are validated at server startup.

Poll the returned job ID until `status` is complete or failed. Job objects always
include `id`, `status` and `source`. The worker transitions queued → running →
complete/failed; complete adds `report` containing an unchanged validated
FinalReport, and failed adds `error: "analysis_failed"`. Three allowlisted input
errors are exposed instead when actionable: `github_mapping_unresolved`,
`github_package_selection_required` and `cutoff_cannot_be_in_the_future`.
Other exceptions never expose provider responses, paths or secrets. An application-level no-score or a
FinalReport with `result_status: "failed"` is still a **complete job**: inspect the
report's result status separately from transport execution status.

The strict report union and examples are maintained in
[`docs/contracts/analysis_contract.schema.json`](../contracts/analysis_contract.schema.json)
and [`examples/analysis/`](../../examples/analysis/). A replacement frontend should
branch on `result_status`, not on truthiness of probability (zero is a valid
prediction). `prediction` is absent in official and no-score states. Read
`mode_used`, `enhanced_status` and `fallback_reason` separately. Preserve source
citations, UTC cutoff, applicability, coverage missingness, report-only exclusions
and independent dependency-impact scope. Synthetic data must remain labelled.

## Failure and execution boundaries

| HTTP code | Error |
| --- | --- |
| 403 | `origin_not_allowed` |
| 404 | `not_found` / `job_not_found` |
| 413 | `body_limit_16KiB` |
| 415 | `application_json_required` |
| 422 | `invalid_submission` |
| 429 | `analysis_queue_full` |

One worker runs at a time, with at most four queued/running jobs and 32 retained
jobs; oldest completed jobs expire first. State is in memory and lost on server
restart. Real orchestrator reports are also persisted under
`--data-dir/reports/analysis`. Polling every 600–1000 ms is sufficient. CORS uses
one explicit origin and rejects unapproved browser POSTs; the server binds only
127.0.0.1. This is a local development adapter without authentication, cancellation,
distributed job storage or a production deployment guarantee.

The browser cannot supply model/replay paths, credentials, data directories or
network execution policy. `--live` explicitly enables current collection;
`--agent-live` explicitly enables provider calls. Otherwise data is offline and
specialists need `--agent-replay`; optional-stage unavailability remains visible
in the report. A registered Model B, historical mapping proofs and a dependency
corpus are still separate deployment dependencies, as in T-027.
