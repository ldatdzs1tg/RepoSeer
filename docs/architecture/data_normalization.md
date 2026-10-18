# Data normalization, deduplication and external relevance (T-018)

T-018 is the offline processing boundary between the T-013/T-014/T-017 collectors
and future T-019 storage/T-020 integration. It computes no features, abandonment
labels, probabilities, coverage census, embeddings or LLM judgments. No network
access or credentials are required.

## Public API and versions

`NormalizationPipeline.process()` accepts canonical `RawRecord` observations, or
separate `repository_records`, `activity_records`, `external_records` iterables.
Sources are optional and independent. `process_jsonl([Path(...)])` uses the existing
`JsonlStorage.iter_records`; an explicitly requested missing file is an error.
Malformed envelopes produce sanitized processing errors. Conflicting contents for
one raw UUID fail explicitly. Identical repeated input UUIDs are counted as repeated
input occurrences, rather than creating new observations.

```python
from pathlib import Path
from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.processing.relevance import VerifiedTargetContext

# Supply only assertions verified upstream, with durable identity/mapping evidence.
target = VerifiedTargetContext(
    package_id="pypi:demo",
    verified_package_url="https://pypi.org/project/demo/",
    verified_aliases=["Demo"],
    mapping_evidence_refs=["verified-identity-manifest:demo"],
)
result = NormalizationPipeline().process_jsonl(
    [Path("data/raw/external/documents.jsonl")], targets=[target],
)
for article in result.canonical_articles:
    consume_article = article.model_dump(mode="json")  # T-019 consumer input
for group in result.deduplicated_document_view:
    consume_group = group.model_dump(mode="json")
for assessment in result.relevance_assessments:
    consume_assessment = assessment.model_dump(mode="json")
```

Input/public output contract remains T-011 **0.1.0**, including all required nullable
keys. `Normalizer.normalize(NormalizationRequest) -> NormalizationResult` is retained.
Internal processing version is `0.1.0-t018`; relevance rule version is
`0.1.0-conservative`. T-014 entities retain their existing strict internal schema;
T-017 input is validated by `validate_external_document_record` (including retained
body hashes), supporting its existing payload versions rather than inventing a
new canonical schema. T-019/T-020 are not implemented here.

## Canonical and internal GitHub output

`GitHubNormalizer.normalize` delegates repository observations to the unchanged
`GitHubRepositoryContractNormalizer`. The legacy `to_dataframe` interface remains.
Numeric IDs define repository identity; a reused name never joins different IDs.
The existing repository adapter describes current observation availability at
retrieval, even when raw historical availability is unknown; this is not historical
proof and is intentionally not rewritten by T-018.

`GitHubActivityNormalizer.normalize_with_entities` uses the shared T-014 extraction
boundary `activity_service.extract_activity_entity`. Its recovery validator remains
compatible. Native provider field mapping is not duplicated. Endpoint/group,
source object identity, raw event timestamp and unknown availability are validated.
Repository identity is the verified numeric ID supplied by the T-014 raw envelope;
when native nested repository ID evidence exists, it must agree. No offline normalizer
can independently re-verify a forged upstream repository assertion without additional
provider evidence.

Canonical outputs include commits with evidenced committed time, issue creation/
closure, PR creation, explicit merge, or explicitly unmerged closure. Current state
does not establish reopen/review events. Closing/merging actors remain unknown;
creation authors are not borrowed as closing actors. Issue/PR object identity uses
native ID, not local issue number. PR keys, including `pull_request=null`, are rejected
from the issue stream. Bot status remains unknown.

Every valid activity observation also has a typed internal entity with its raw ID,
actual run ID, retrieval time and outcome. Contributor aggregates, GitHub releases,
and commits without committed time remain internal-only. Because the unchanged
T-011 pair validator requires every processed input to have canonical output, these
inputs have an explicit `unsupported_record_type` error at the canonical boundary.
They remain accepted internally: `internal_only_observation_count` and
`canonical_rejected_observation_count` distinguish this representational limitation
from an invalid observation. GitHub releases never become PyPI releases.

Pipeline error accounting keeps these meanings separate:

| Outcome | Meaning |
| --- | --- |
| Canonical boundary rejection | The unchanged T-011 result cannot represent the input; its error and rejection counters are retained. |
| Accepted internal-only observation | A valid T-014 entity is retained with an explicit outcome and counted as accepted, without canonical output. |
| Actual processing error | A validation/unsupported-source failure remains in `processing_errors`. |
| Raw observation rejection | An input has neither valid canonical nor valid internal output; counted in `rejected_observation_count`. |

Expected boundaries are excluded from `processing_errors` only when the raw ID has
an accepted internal entity, the error code is `unsupported_record_type`, and its
message equals that entity's `contributor_aggregate`, `github_release` or
`event_time_unknown` outcome. All other errors remain visible. T-019/T-020 must not
interpret the sum of canonical result errors as raw source failures.

## Article normalization

Only validated T-017 `external_document` captures become canonical Articles;
discovery candidates are rejected. HTML identity uses the saved final document URL;
native API identity uses its represented item URL, never the fetch API endpoint.
Initial candidate URL remains `original_url`. T-011 URL canonicalization preserves
HTTP/HTTPS, path case, meaningful trailing slashes and nontracking query parameters.
The legacy DataFrame URL method now uses this same helper.

Missing title is a structured rejection, never an invented title. Unknown authors
and language remain null; publication precision stays timestamp/date/unknown without
midnight fabrication. Validated NFC/LF content and SHA-256 are preserved. Unavailable
content keeps null text/hash/availability. Kind defaults to `other` when not evidenced.
Repository/package/technology associations remain empty; query scope and captured
repository scope do not establish relevance.

Provenance refers to exactly the input observation, with original source, URL,
retrieval, raw availability, and `valid_at=retrieved_at`, `temporal_basis=observation`.
No publication or commit timestamp becomes historical availability.

## Logical grouping and observation history

Normalization partitions records by actual collection run, source and record kind.
Every canonical request/result pair is validated before cross-run grouping. Request
IDs are deterministic UUIDv4-shaped values derived from the partition; collection run
IDs are never invented or replaced. Canonical records retain individual provenance.

The typed result exposes canonical normalization results and derived repository,
activity and Article lists; internal activity entities; deduplicated repository,
activity and document views; exact content-duplicate relations; assessments; lineage;
errors, diagnostics and summary counts. It avoids storing redundant full raw payloads.
Canonical lists preserve observations, rather than claiming one current row per object.

Grouping metadata retains every contributing raw ID, source, source URL, actual run,
retrieval timestamp, content hash and normalized state hash. GitHub logical groups use
numeric repository ID plus native object identity. Contributors additionally use
observation time; anonymous observations are never merged by an invented account ID.
Changed object metadata remains distinct in state hashes and original observations.

Document identity is URL-derived Article ID; revision identity is content hash.
Unavailable observations do not create a synthetic content revision. Primary selection
prefers valid full content, then latest retrieval, then ascending raw ID. This is a
current view only. Identical text at different URLs retains distinct Articles plus
an auditable content-hash relation. Near-duplicate identity remains `unverified`.
Output/group ordering is deterministic and repeated processing is idempotent.

## Target-specific relevance

`VerifiedTargetContext` requires canonical package identity. Repository matching also
requires numeric repository ID, canonical root URL and upstream mapping evidence.
Verified aliases/package URLs require evidence references. These inputs are trusted
assertions from an identity verifier; nonempty strings do not independently verify
mapping authenticity. Missing mapping allows package-only assessment, with no guessed
repository identity. An optional existing `FunctionalProfileResult` is consumed,
never generated from article text.

Rules use normalized content, explicit package identifiers/verified URLs and aliases
with package/library/framework or usage context. A general technical word alone does
not disambiguate an alias. Main-body HTML link evidence must align with substantive
normalized prose; footer/navigation/aside/header links are separately auditable and
cannot establish direct relevance. Link-only evidence has no fabricated text offset.

Function matching conservatively requires an exact phrase of at least three words
from grounded official-source profile claims, matching target identity and package
scope. Repo-only, foreign or unsupported profiles cannot authorize function relevance.
Alternative context requires explicit comparison/migration and the verified use case
in the substantive target context. This is a deliberately narrow lexical policy,
not calibrated semantic similarity. Generic Python/package/framework discussion does
not establish target relevance. The compatible `technology_level` literal remains,
but positive technology-level relevance is not emitted without a separate verified
technology-to-target association policy. Query labels are not decision inputs.

Assessments distinguish `relevant`, `irrelevant`, `unresolved` and the independent
context level. Missing content, ambiguous aliases and unverified function claims remain
unresolved. Irrelevant Articles remain in canonical output and retain their assessments.
The same observation can have different results for different packages. Assessment
raw IDs identify the particular revision/observation, not just the document URL.

Positive assessments require substantive target evidence. Structural validation alone
checks the schema, decisions, context and span lengths; it does not prove that the
quoted content exists. Evidence verification is explicit and revision-aware:

```python
from reposeer.processing.relevance import (
    RelevanceAssessment, validate_assessment_evidence,
)

assessment = RelevanceAssessment.model_validate(data)
validate_assessment_evidence(assessment, matching_article)
```

The helper revalidates the canonical Article and assessment, checks Article identity,
content hash and exact raw references, then verifies every offset/bounds and exact
NFC/LF content slice. It does not mutate either object or store a trusted boolean.
Standalone assessments serialize/reload without Article context; consumers must run
the helper before treating their evidence as grounded.

Full pipeline results automatically verify evidence using the included canonical
Articles, including on reload:

```python
from reposeer.pipelines.normalization import NormalizationPipelineResult

serialized = result.model_dump(mode="json")
restored = NormalizationPipelineResult.model_validate(serialized)
assert restored.model_dump(mode="json") == serialized
```

Matching uses `(article_id, content_sha256, sorted(raw_record_ids))`, so different
observations/revisions of one URL cannot lend each other evidence. Missing matching
Articles, conflicting Article objects at the same observation key, and altered
spans/revisions/raw references are rejected with stable reason codes.

Rules scan at most 200,000 content characters/1,000 sentences and 2,000,000 HTML
characters/500 links. Truncation cannot establish a confirmed negative. No numeric
confidence or independent precision/recall claim is produced.

## Reference-only and substantive relevance

Verified URLs, aliases, functional phrases and migration claims must occur in
substantive body content to support relevance. Dedicated References, Bibliography,
Further Reading, Related Links and Sources sections (including inline reference
entries), footer/navigation blocks and HTML reference containers cannot provide
positive evidence. A Markdown heading at the same or higher level can resume body
content; ordinary prose containing "references" does not start a reference section.
Body evidence remains eligible when references repeat the same URL or text.
Relevance decisions are candidate associations, not maintenance labels or predictive
features.

The evaluator checks each occurrence against plain-text section boundaries and,
when supplied, original HTML block context. Uncertain body/reference alignment
returns `unresolved/reference_context_ambiguous`; clearly reference-only content
returns `irrelevant/reference_only_link` or `reference_only_mention`. Canonical
Article text, hashes and evidence offsets remain unchanged. Rules remain bounded
heuristics, not a complete Markdown or HTML parser. This correctness fix preserves
rule version `0.1.0-conservative` and the public serialization shape.

Serialization/reload validates Article identity, hashes, raw references and exact
evidence spans. It does **not** revalidate original DOM/link context without the
raw HTML; rerun the evaluator with that observation for a context-aware decision.

## Lineage and temporal limitations

Article/raw-keyed lineage sidecars retain capture identity separately from discovery
provider/native IDs and compact references/query associations, including available
query/window/version metadata from the nested immutable discovery observation.
Missing associations are not reconstructed. Incomplete lineage remains incomplete.

An optional cutoff produces diagnostics only: unknown availability, observed after
cutoff, or requires downstream D06 policy. It neither filters/backdates records nor
asserts historical eligibility. Current content publication dates do not establish
its historical revision availability. Full historical mapping/document reconstruction,
D06, E1/E2 and production profiling remain future work.

## Verification

All artifacts go to the sibling `RepoSeer_T018_Verification`, never into source or
real raw datasets. From Windows CMD at repository root, using the existing environment:

```bat
set "T018_VERIFY=%CD%\..\RepoSeer_T018_Verification"
if not exist "%T018_VERIFY%\temp" mkdir "%T018_VERIFY%\temp"
if not exist "%T018_VERIFY%\pytest-temp" mkdir "%T018_VERIFY%\pytest-temp"
set "TEMP=%T018_VERIFY%\temp"
set "TMP=%TEMP%"
set "TMPDIR=%TEMP%"
set "RUFF_CACHE_DIR=%T018_VERIFY%\ruff-cache"
set "PYTHONPYCACHEPREFIX=%T018_VERIFY%\python-cache"
set "UV_CACHE_DIR=%T018_VERIFY%\uv-cache"
set "PYTHONDONTWRITEBYTECODE=1"
python -m pytest -q tests/unit/processing/test_normalization.py tests/integration/test_normalization_pipeline.py --basetemp "%T018_VERIFY%\pytest-temp\t018-focused" -o "cache_dir=%T018_VERIFY%\pytest-cache"
python -m pytest -q -m "not smoke" --basetemp "%T018_VERIFY%\pytest-temp\full-regression" -o "cache_dir=%T018_VERIFY%\pytest-cache"
python -m ruff check .
python scripts/export_contract.py --check
python scripts/validate_examples.py
git diff --check
```

Use a fresh basetemp leaf for each repeated test invocation; pytest clears its own
basetemp. Reports distinguish synthetic fixtures/manual acceptance labels from
bounded, read-only real collection samples. Reusing acceptance labels illustrates
rule behavior; it is not an independent benchmark or threshold calibration.
