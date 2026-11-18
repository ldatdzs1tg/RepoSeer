# D07 specialist agents — contract 0.1.0

## A. Objective

**Soft intake — Evidence enrichment — Hard promotion.** The same **three** v3
specialists serve two purposes: research/enrichment from incomplete D03–D05 inputs,
and structured signal extraction from D06-eligible evidence. These are two information
lanes, not two teams or an additional agent. This T-024 module defines contracts,
pure validation, schemas and synthetic examples; it implements no LLM execution,
crawling, orchestration, training or inference.

The [v3 architecture](../architecture/pipeline.md) and
[analysis contract](analysis_contract.md) remain authoritative for cutoff, labels,
coverage, model selection and FinalReport. Research can retain leads that cannot yet
satisfy these gates; it cannot relax them.

## B. Three specialist roles

| Specialist (`agent_kind`) | Research/enrichment | Strict signal extraction |
| --- | --- | --- |
| **Maintenance & Lifecycle** (`maintenance_lifecycle`) | Interpret available maintenance context; distinguish stable, low-change software from possible cessation. Identify missing maintainer notices, releases or issue history and suggest sources to inspect. | Direct-package maintenance concerns, support reduction, departure, deprecation, transition and end-of-support/replacement claims. Official direct-package text routes here. |
| **Functional Trend & Demand** (`functional_trend_demand`) | Explain what problem a package addresses, its use cases and functional context. Read related discussions and request further official documentation or external discussions. | Same-function/use-case discussion, emerging use cases and changing technical needs; never infer package user/adoption counts from discussions. |
| **Alternatives & Migration** (`alternatives_migration`) | Propose source-backed alternative candidates, distinguish functional similarity from actual migration, identify unverified compatibility and request migration/comparison evidence. | Comparisons, recommendations, explicit migration, complaints and reported official replacement claims. A recommendation is not a migration. |

## C. Research lane

All six research models inherit `ContractModel`: strict types, `extra="forbid"`,
and every declared field required, including nullable fields. They use the existing
`Specialist`, `NonEmpty`, `UtcTimestamp`, package and repository identity types.

- `ResearchCandidate` carries a request-local `candidate_id` and an upstream
  storage/manifest `source_ref`, not a source ID invented by an LLM. URL, topic,
  text and all three timestamps may be null. An excerpt is at most 10,000 characters.
  Source kind may be unknown and relevance unresolved/not assessed. Missing or
  postcutoff availability does **not** discard a research candidate.
- `ResearchHandoffRequest` binds run, request, specialist, package and cutoff.
  Repository mapping may be unresolved (`repo_id=null`). An optional description
  is limited to 5,000 characters and requires `description_source_refs`. Requests
  accept 0–50 candidates with unique candidate IDs and at most ten research questions.
  A topic hint need not match a strict route: uncertainty may require reclassification.
- `ResearchInsight` carries a statement, at least one declared source reference and
  a mandatory limitation. It is an interpretation for research, not a verified fact.
- `InformationGap` identifies missing information and a suggested question. Related
  source references may be empty when no sources are available.
- `EnrichmentSuggestion` gives an objective, rationale, optional query hint and related
  references for **D03** (package understanding/official functional documentation),
  **D04** (search/query planning) or **D05** (retrieval, enrichment and relevance).
  Query hints are suggestions, not trusted executable API instructions.
- `ResearchHandoffResponse` returns insights, gaps and suggestions with
  `generated_at >= cutoff_at`. As with strict `AgentResponse`, `complete` requires
  `error_code=null`; `partial` and `failed` require an error code. Failed responses
  cannot contain insights, though they can explain gaps and recovery suggestions.

`validate_research_handoff_pair(request, response)` fully revalidates both models,
including mutated model instances, matches run/request/agent/package/cutoff, rejects
duplicate insight/gap/suggestion IDs, and checks **all** output source references
against the union of candidate `source_ref` values and `description_source_refs`.
Candidate IDs and arbitrary URLs are not substitutes for those references. Without
candidates or a description, a response must contain a gap or suggestion and cannot
invent insights. An empty research request is valid for research planning.

Research `complete` means request processing finished; it proves neither complete
source coverage nor verification. No probability, risk level, label, feature value,
official status or numeric confidence field is allowed. Neither crawled content nor
LLM-provided URLs/instructions are automatically executed. Source references must be
verified by the upstream/trusted producer; structural validation cannot prove a
stored record exists, relevance is correct or a timestamp establishes availability.

## D. Strict validated-signal lane

The existing `D06Gate`, `D06Evidence`, `AgentRequest`, `SignalCitation`,
`ExtractedSignal`, `AgentResponse` and `ConflictReviewRecord` wire formats remain
**0.1.0**. Research models do not replace them.

`AgentRequest.evidence` still requires 1–50 `D06Evidence` passages. Each carries a
concrete source revision/content hash, original-event ID, exact text/hash/absolute
offsets and required availability, validity and retrieval times. The deterministic
router checks topic, context and package; official direct-package text routes only
to Maintenance & Lifecycle. The source revision must be available and valid by cutoff;
event time cannot follow cutoff. Producers must authenticate the `D06Gate` receipt
against saved trusted D06 decisions, including historical D03/profile, D04/query and
D05/document revisions. Self-declared eligibility is insufficient.

`AgentResponse` contains provisional `ExtractedSignal` objects, timestamps, exact
`SignalCitation` offsets and source revisions, uncertainty, and agent/model/prompt
versions. `validate_agent_pair` rechecks identity, source/subject lineage, exact quoted
text at Unicode code-point offsets, source availability and gate timing. `proposed_use`
retains its original meanings:

- `feature_candidate`: eligible for further review, not automatically an E2 feature.
  Report/status-only inputs and known-outcome/official replacement claims cannot
  be promoted; future events/announcements/effective dates cannot be predictive features.
- `status_candidate`: Maintenance & Lifecycle only, backed by direct official input,
  solely a lead for independent, uniform **D09** verification. `announced_at` must not
  follow cutoff. A future `effective_at` remains valid for a scheduled cessation plan.
- `report_only`: contextual information excluded from predictive features.

`available_at` describes when a source revision could be known; `announced_at` is
when a notice was published; `effective_at` is when a plan takes effect. These times
are not interchangeable. No specialist assigns verified official status, ground-truth
labels or Model A/B probabilities.

`ConflictReviewRecord` records coordinator decisions. Existing
`validate_conflict_reviews(records)` still enforces one decision per
`(analysis_run_id, cluster_id)`. The additive
`validate_review_references(records, responses)` also revalidates response structure,
rejects duplicate signal IDs across responses in the same run, finds every reviewed
signal in that run, matches subjects and requires the review source set to exactly
equal the union of the referenced signals' citations. **Responses must first have
passed `validate_agent_pair` with their original requests**; this helper cannot
reconstruct missing D06 evidence or prove semantic truth/final E2 eligibility.

Clustering/deduplication uses subject, action, date and original event/source so
syndication or multiple agents do not multiply events. A conflict permits at most
one bounded reviewer LLM call per cluster followed by deterministic revalidation.
`unresolved` remains report-only; `rejected` is audit-only. `review_calls` and
`deterministic_recheck_passed` are **coordinator records**, not independently verified
proof of execution from JSON. T-026 must actually enforce the call budget and rerun
source/entity/quote/time checks; the reviewer is not a fourth specialist.

## E. Promotion rules

```text
ResearchCandidate
    -> enrichment/retrieval/relevance reassessment
    -> persisted source revision
    -> D06 eligibility
    -> AgentRequest / AgentResponse
    -> validated signal
    -> D08 feature eligibility OR report-only OR D09 verification lead
```

There is no implicit conversion from `ResearchCandidate`/`ResearchInsight` to
`D06Evidence`, `ExtractedSignal`, `ValidatedAgentEvent`, `AnalysisEvidence`, a model
feature or official status. Research descriptions also do not become verified D03
functional profiles. After trusted retrieval and checks, a producer must construct
new strict inputs from the persisted revisions.

Only cutoff-safe, non-outcome signals with review and sufficient per-source/window
eligible-versus-processed coverage may reach D08/E2. E1 remains rule-derived and
never consumes specialist outputs. D09 searches and verifies uniformly across the
cohort, independently of agent hits. T-025's trusted backend verifiers for historical
documents, census, agent events and processing coverage remain required. LLM flags
cannot replace them. Any later mapping to T-021 report evidence requires its own
provenance and use checks; frozen model probabilities cannot be edited by explanations.

## F. Integration handoff and artifacts

T-026 may execute the same three specialists on research or strict requests, route
suggestions back through an orchestrator to D03–D05, bound feedback iterations and
query budgets, perform retrieval and trusted source verification, and implement
clustering/conflict review. None of those runtime operations is implemented here.

The canonical source is [`specialist_agents.py`](../../src/reposeer/contracts/specialist_agents.py).
Generated Draft 2020-12 schemas cover [strict input](specialist_agent_input.schema.json),
[strict output](specialist_agent_output.schema.json),
[conflict review](specialist_conflict_review.schema.json),
[research request](specialist_research_request.schema.json) and
[research response](specialist_research_response.schema.json). JSON Schema checks
structure; Python pair/lineage validators check cross-document invariants.

The seven original [examples](../../examples/agents/) remain unchanged. Their three
strict pairs remain valid. `conflict_review.json` is a standalone structural example:
its second referenced signal is not supplied as a response, so it does not claim a
successful lineage check. The new synthetic `research_request.json` and
`research_response.json` demonstrate an unresolved candidate with null source dates,
one insight with limitations, a gap and a D03 enrichment suggestion. Synthetic manifest
references and `.test` URLs make no claim about real stored records or source dates.

```powershell
uv run --frozen python -m scripts.export_specialist_agents --check
uv run --frozen python -m scripts.validate_specialist_examples
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q tests/contracts/test_specialist_agents.py
```

## G. Limitations

Contracts cannot guarantee semantic correctness of LLM extraction, authenticate a
source from a JSON reference, or establish historical availability from a self-declared
timestamp. A few discussions do not measure population-wide demand. Explanations
alone do not improve Model A. Using E2 for predictions still requires historical
validation, separate Model B evaluation, coverage, calibration and deployment gates.

### Audit of the original T-024 boundary

| Check | Finding and action |
| --- | --- |
| A. Strict intake | Confirmed: `AgentRequest.evidence` requires at least one `D06Evidence` with non-null availability. Preserve it and add separate soft intake. |
| B. Research outputs | Confirmed gap: no research handoff/insight/gap/suggestion contract existed. D03 grounded profiles and D05 relevance assessments are not equivalent; add the six research models here. |
| C. Announcement time | Confirmed: strict status candidates previously lacked an announced-by-cutoff guard. Add it, retaining future effective dates for plans. |
| D. Review lineage | Confirmed gap: the existing helper checked records and cluster uniqueness only. Add response-based lineage checks without changing its signature. Runtime receipts still require T-026 enforcement. |
| E. Agent README | Confirmed: the old README described Repository Health, Technology Trend, News Sentiment and a risk-scoring Synthesizer. Replace it with the three v3 roles and contract link. |
| F. T-025 verifier | Confirmed existing protection: `EvidenceVerifier` and its document/census/event/processing checks require trusted backend dependencies. Preserve T-025 unchanged. |

At audit time, strict model/helper imports were confined to the specialist export
and example-validation scripts and `tests/contracts/test_specialist_agents.py`;
there was no implemented multi-agent runtime consuming them.
