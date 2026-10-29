# D07 specialist agents — design contract 0.1.0

This short design follows `RepoSeer_End_to_End_Pipeline_Final_v3 (1).md`, D06–D08.
It defines **exactly three** specialists. The existing repository-health/news-sentiment
agent README describes an older plan; this contract uses the v3 roles. This is an I/O
boundary and synthetic demonstration, not a running LLM workflow or a trained model.

| Specialist (`agent_kind`) | Receives D06-eligible passages | Produces provisional signal types |
| --- | --- | --- |
| **1. Maintenance & Lifecycle** (`maintenance_lifecycle`) | Direct-package maintenance text; official GitHub/PyPI README, changelog, release, maintainer notice or external text, all with as-of proof | Maintenance concern, support reduction, maintainer departure, deprecation, official transition, end-of-support or replacement **claim** |
| **2. Functional Trend & Demand** (`functional_trend_demand`) | External same-function/use-case passages | Emerging use case, changing technical need, function discussion; never package user/adoption counts |
| **3. Alternatives & Migration** (`alternatives_migration`) | External alternative/comparison/migration passages | Comparison, recommendation, explicit migration, complaint, reported official replacement; a recommendation is not a migration |

The deterministic router selects by `topic`, `context_level`, package identity and D06
decision. Official direct-package text routes to Agent 1. Passage text has a stable
source revision, original-event ID, absolute offset, source availability time, and a
`D06Gate` receipt. The request rejects a revision unavailable at `cutoff_at`, a future
event timestamp, a wrong topic/context, or more than 50 passages. An `eligible` receipt
must be checked against D06's saved decision by the producer; self-declared JSON alone
cannot authenticate the upstream gate. D03 profile/query and D05 content revisions
must already have passed their historical checks before D06 emits the receipt.

The agent returns `ExtractedSignal` objects with subject, scope, type, event/announcement/
effective times, exact quoted offsets, source revision and availability, uncertainty,
plus agent/model/prompt versions. `proposed_use` is provisional. The pair validator
checks request IDs, route, source IDs, revisions, timestamps, and exact quoted passage
bytes at absolute offsets. Unsupported claims are rejected with an audit reason.
Report-only or status-candidate input cannot be promoted to a predictive feature.
End-of-support/official replacement claims cannot become E2 features. An Agent 1
`status_candidate` is only a lead for D09's independent, uniform verifier. No agent
emits a ground-truth label, official known status, risk score, or probability.

The coordinator clusters by subject + action + date + original event/source, merging
syndicated copies and corroborating agents without counting them as multiple events.
It uses **at most one bounded reviewer LLM call per conflicting cluster**, showing
only conflicting passages and their original source revisions. One review decision
per `(analysis_run_id, cluster_id)` is enforced by `validate_conflict_reviews`. After review, code
reruns quote, source, entity and temporal checks. `unresolved` becomes `report_only`,
excluded from D08/E2; `rejected` is audit-only. The reviewer is not a fourth specialist.
Only validated, cutoff-safe, non-outcome signals can feed E2, subject to D08's
per-source/window eligible-versus-processed coverage gate. D09 finds official events
uniformly across the cohort, independent of Agent 1 hits. E1 remains rule-derived and
never consumes specialist outputs. Neither agent response nor conflict review can
change a frozen Model A/B probability directly.

After verification, the report assembler can map Agent 1/2/3 signals to the existing
`AnalysisEvidence` maintenance/functional_demand/migration-or-context classes with
`origin="agent"` and source/agent provenance. `feature_candidate` only becomes
`use="feature"` after review and D08 coverage; `event_candidate` goes to D09 and stays
report-only until an independent official ledger decision. These examples do not
claim that conversion or inference is implemented.

The Python source is [`specialist_agents.py`](../../src/reposeer/contracts/specialist_agents.py).
Generated structural schemas are
[`input`](specialist_agent_input.schema.json),
[`output`](specialist_agent_output.schema.json), and
[`conflict review`](specialist_conflict_review.schema.json). Cross-document and semantic
checks require `validate_agent_pair`; JSON Schema alone checks structure. The three
synthetic [response examples](../../examples/agents/) each have a matching request;
`conflict_review.json` illustrates a one-call unresolved conflict. The example URLs
use `.test` and make no empirical claim.

```powershell
uv run --frozen python -m scripts.export_specialist_agents --check
uv run --frozen python -m scripts.validate_specialist_examples
uv run --frozen pytest -q tests/contracts/test_specialist_agents.py
```
