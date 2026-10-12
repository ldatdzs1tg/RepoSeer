# GitHub Activity Entity Schemas — T-014 Phase 1

## 1. Purpose and relationship to T-011

`src/reposeer/schemas/github/activity_entities.py` defines five strict internal
extraction/validation models for T-014 collectors and future T-018 normalization.
They do not replace the canonical T-011 `RawRecord`, do not inherit from
`RepositoryActivity`, and are not registered in `NormalizedRecord` or the public
exchange contract. Canonical contract version **0.1.0** remains unchanged; its
Python models, generated JSON Schema and TypeScript definitions remain authoritative.
Legacy GitHub DTOs remain available to their existing collectors without changes.

RepoSeer's prediction unit is `(package_id, cutoff_at)`. A GitHub repository supplies
linked entities/activity; it does not replace the package as the prediction unit.
Historical data spans 2019–2025, with quarterly prediction cutoffs during 2020–2024
and feature lookback windows of 3, 6 and 12 months. These models do not calculate
features or determine 12-month maintenance abandonment labels.

Import internal models directly from `reposeer.schemas.github.activity_entities`.
See [T-011 data contract](data_contract.md), [raw envelope](raw_record.md) and
[canonical processed entities](processed_entities.md) for the exchange boundaries.

## 2. Shared conventions

All models inherit a private `_ActivityEntity` configured with `extra="forbid"`,
`strict=True` and `allow_inf_nan=False`. They do not inherit legacy `BaseSchema`.
**Every declared key is structurally required**, including nullable keys. No field
has an implicit default, and no missing value becomes zero, false, an empty list or
an observation timestamp. Unknown fields are rejected.

The following aliases are reused unchanged from `reposeer.contracts.models`:

| Type | Constraint |
| --- | --- |
| `RepoId` | `github:repository:<positive decimal GitHub repository ID>` |
| `DecimalId` | Positive decimal string, no leading zeros; native numeric IDs must be explicitly extracted as strings |
| `UtcTimestamp` | Valid UTC string `YYYY-MM-DDTHH:mm:ss.sssZ`, exactly three fractional digits |
| `Count` | Strict integer from 0 through `9007199254740991` (`2^53 - 1`) |
| `NonEmpty` | String with at least one non-whitespace character |
| `HttpUrl` | HTTP(S) string using canonical URL validation, including rejection of credentials/whitespace |

The reusable `GitSha` alias is `Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]`.
Only full lowercase SHA-1 identities are supported in this phase; shortened SHAs,
uppercase hexadecimal and other hash lengths are rejected. Issue and PR numbers
are strict integers from 1 through `2^53 - 1`, independently of their numeric IDs.

Nullable fields use `T | None` without defaults. `null` means unknown/not extracted;
`[]` means a list was observed and is known empty. String numbers, string booleans,
bool-as-integer values, naive datetime objects and noncanonical timestamps are not
coerced. The future extraction adapter must convert valid source timestamps to
canonical strings, supply every key explicitly, and handle source empty text according
to the `NonEmpty | None` field constraint while retaining the original raw text.
No contributor email is an entity field.

## 3. Entity schema: GitHubCommit

Identity: `(repo_id, commit_sha)`.

| Field | Python type | Required | Nullable | Native GitHub mapping | Semantics |
| --- | --- | --- | --- | --- | --- |
| `repo_id` | `RepoId` | Yes | No | Verified repository context | Stable numeric repository identity |
| `commit_sha` | `GitSha` | Yes | No | `sha` | Full commit identity |
| `author_github_id` | `DecimalId \| None` | Yes | Yes | `author.id` | GitHub author ID when linked |
| `author_login` | `NonEmpty \| None` | Yes | Yes | `author.login` | Observed author login |
| `committer_github_id` | `DecimalId \| None` | Yes | Yes | `committer.id` | GitHub committer ID when linked |
| `committer_login` | `NonEmpty \| None` | Yes | Yes | `committer.login` | Observed committer login |
| `authored_at` | `UtcTimestamp \| None` | Yes | Yes | `commit.author.date` | Source author date, not publication evidence |
| `committed_at` | `UtcTimestamp \| None` | Yes | Yes | `commit.committer.date` | Source committer date, not public push time |
| `message` | `NonEmpty \| None` | Yes | Yes | `commit.message` | Commit message when extracted |
| `parent_shas` | `list[GitSha] \| None` | Yes | Yes | `parents[].sha` | Known parents; null means unknown |
| `is_merge_commit` | `bool \| None` | Yes | Yes | Derived only from known parent list | True exactly when more than one parent is known |
| `additions_count` | `Count \| None` | Yes | Yes | Commit detail `stats.additions` | Null unless actually collected |
| `deletions_count` | `Count \| None` | Yes | Yes | Commit detail `stats.deletions` | Null unless actually collected |
| `html_url` | `HttpUrl \| None` | Yes | Yes | `html_url` | Observed commit page URL |

Missing GitHub author/committer identity is valid when the corresponding values are
null. If `parent_shas=None`, `is_merge_commit` must be null. A known parent list requires
`is_merge_commit == (len(parent_shas) > 1)`, including false for an empty list.
Duplicate or invalid parent SHAs are rejected. No ordering is imposed between author
and committer dates; Git dates can differ or be rewritten.

## 4. Entity schema: GitHubIssue

Identity: GitHub numeric `issue_id`; `issue_number` is the repository-local number.

| Field | Python type | Required | Nullable | Native GitHub mapping | Semantics |
| --- | --- | --- | --- | --- | --- |
| `repo_id` | `RepoId` | Yes | No | Verified repository context | Stable repository identity |
| `issue_id` | `DecimalId` | Yes | No | `id` | Stable issue identity |
| `issue_number` | `int` | Yes | No | `number` | Strict positive repository-local number, max `2^53 - 1` |
| `title` | `NonEmpty` | Yes | No | `title` | Observed title |
| `state_at_retrieval` | `Literal["open", "closed"]` | Yes | No | `state` | Current observed state |
| `created_at` | `UtcTimestamp` | Yes | No | `created_at` | Required source creation timestamp |
| `author_github_id` | `DecimalId \| None` | Yes | Yes | `user.id` | Author numeric ID when available |
| `author_login` | `NonEmpty \| None` | Yes | Yes | `user.login` | Observed author login |
| `body` | `NonEmpty \| None` | Yes | Yes | `body` | Observed body when extracted |
| `state_reason_at_retrieval` | `NonEmpty \| None` | Yes | Yes | `state_reason` | Current state reason, not historical evidence |
| `updated_at` | `UtcTimestamp \| None` | Yes | Yes | `updated_at` | Last source update when known |
| `closed_at` | `UtcTimestamp \| None` | Yes | Yes | `closed_at` | Source close timestamp when known |
| `comments_count` | `Count \| None` | Yes | Yes | `comments` | Count observed at retrieval |
| `label_names` | `list[NonEmpty] \| None` | Yes | Yes | `labels[].name` | Observed labels; null differs from empty |
| `html_url` | `HttpUrl \| None` | Yes | Yes | `html_url` | Observed issue page URL |

When present, `updated_at` and `closed_at` must each be at least `created_at`.
A closed issue may have an unknown/null close timestamp. Current state does not
establish state at any historical cutoff. The GitHub `/issues` endpoint also returns
PR objects: the future collector must **discard items containing the `pull_request`
key from the issues dataset**, even if that key's value is null. Endpoint filtering
is not implemented by these entity schemas.

## 5. Entity schema: GitHubPullRequest

Identity: GitHub numeric `pr_id`; `pr_number` is the repository-local number.

| Field | Python type | Required | Nullable | Native GitHub mapping | Semantics |
| --- | --- | --- | --- | --- | --- |
| `repo_id` | `RepoId` | Yes | No | Verified repository context | Stable repository identity |
| `pr_id` | `DecimalId` | Yes | No | `id` | Stable PR identity |
| `pr_number` | `int` | Yes | No | `number` | Strict positive repository-local number, max `2^53 - 1` |
| `title` | `NonEmpty` | Yes | No | `title` | Observed title |
| `state_at_retrieval` | `Literal["open", "closed"]` | Yes | No | `state` | State observed at retrieval |
| `created_at` | `UtcTimestamp` | Yes | No | `created_at` | Required source creation timestamp |
| `author_github_id` | `DecimalId \| None` | Yes | Yes | `user.id` | Author ID when available |
| `author_login` | `NonEmpty \| None` | Yes | Yes | `user.login` | Observed author login |
| `body` | `NonEmpty \| None` | Yes | Yes | `body` | Observed body when extracted |
| `is_merged` | `bool \| None` | Yes | Yes | `merged` when supplied; otherwise consistent `merged_at` evidence | False only with verified unmerged evidence; null when unknown |
| `is_draft_at_retrieval` | `bool \| None` | Yes | Yes | `draft` | Observed draft status |
| `updated_at` | `UtcTimestamp \| None` | Yes | Yes | `updated_at` | Last source update when known |
| `closed_at` | `UtcTimestamp \| None` | Yes | Yes | `closed_at` | Source close timestamp when known |
| `merged_at` | `UtcTimestamp \| None` | Yes | Yes | `merged_at` | Source merge timestamp when known |
| `head_sha` | `GitSha \| None` | Yes | Yes | `head.sha` | Full observed head SHA |
| `base_sha` | `GitSha \| None` | Yes | Yes | `base.sha` | Full observed base SHA |
| `comments_count` | `Count \| None` | Yes | Yes | `comments` | Current issue-style comment count |
| `review_comments_count` | `Count \| None` | Yes | Yes | `review_comments` | Current review comment count |
| `label_names` | `list[NonEmpty] \| None` | Yes | Yes | `labels[].name` | Observed labels; null differs from empty |
| `html_url` | `HttpUrl \| None` | Yes | Yes | `html_url` | Observed PR page URL |

Each known update, close or merge timestamp must be at least `created_at`.
A non-null `merged_at` requires `is_merged=True` and `state_at_retrieval="closed"`.
Conversely, `is_merged=True` requires `merged_at`. With null merge time, false means
verified unmerged and null means unknown. Closed does not imply merged, and a closed
PR need not have a known `closed_at`. No reopen or review timeline is inferred.

## 6. Entity schema: GitHubContributorObservation

This is a **current aggregate observation**, not a historical contribution event.
Known-contributor logical identity is `(repo_id, contributor_github_id, observed_at)`.
Anonymous observations have no invented GitHub numeric identity; raw/provenance
observation identity belongs to the envelope.

| Field | Python type | Required | Nullable | Native GitHub mapping | Semantics |
| --- | --- | --- | --- | --- | --- |
| `repo_id` | `RepoId` | Yes | No | Verified repository context | Stable repository identity |
| `is_anonymous` | `bool` | Yes | No | Sufficient contributor response evidence | Explicit anonymous status; no arbitrary one-field inference |
| `contributions_count` | `Count` | Yes | No | `contributions` | Current aggregate, strict nonnegative integer |
| `observed_at` | `UtcTimestamp` | Yes | No | Canonical `RawRecord.retrieved_at` | Actual observation time, supplied by extraction |
| `contributor_github_id` | `DecimalId \| None` | Yes | Yes | `id`, if present | Known user ID; null for anonymous contributors |
| `login` | `NonEmpty \| None` | Yes | Yes | `login`, if present | Observed login; null for anonymous contributors |
| `user_type` | `NonEmpty \| None` | Yes | Yes | `type`, if present | Observed user type, without inferred bot labels |
| `html_url` | `HttpUrl \| None` | Yes | Yes | `html_url`, if present | Observed contributor page URL |

Anonymous contributors require both ID and login to be null. Non-anonymous
contributors require a valid numeric ID; their login may still be unknown.
There is no email, `committed_at`, `event_at` or `cutoff_at` field. `observed_at` equality
with raw retrieval is an extraction/integration requirement: an entity alone cannot
validate against an envelope that was not supplied to it. Current contribution counts
cannot be backfilled to historical cutoffs; downstream historical contributor activity
must come from commit/event evidence.

## 7. Entity schema: GitHubRelease

Identity: GitHub numeric `release_id`.

| Field | Python type | Required | Nullable | Native GitHub mapping | Semantics |
| --- | --- | --- | --- | --- | --- |
| `repo_id` | `RepoId` | Yes | No | Verified repository context | Stable repository identity |
| `release_id` | `DecimalId` | Yes | No | `id` | Stable GitHub release identity |
| `tag_name` | `NonEmpty` | Yes | No | `tag_name` | Associated tag name |
| `is_draft` | `bool` | Yes | No | `draft` | Explicit source draft flag |
| `is_prerelease` | `bool` | Yes | No | `prerelease` | Explicit source prerelease flag |
| `name` | `NonEmpty \| None` | Yes | Yes | `name` | Observed release name |
| `body` | `NonEmpty \| None` | Yes | Yes | `body` | Observed release text |
| `author_github_id` | `DecimalId \| None` | Yes | Yes | `author.id` | Author numeric ID when available |
| `author_login` | `NonEmpty \| None` | Yes | Yes | `author.login` | Observed author login |
| `created_at` | `UtcTimestamp \| None` | Yes | Yes | `created_at` | Source timestamp, not necessarily publication-object creation |
| `published_at` | `UtcTimestamp \| None` | Yes | Yes | `published_at` | Publication timestamp when evidenced; otherwise null |
| `target_commitish` | `NonEmpty \| None` | Yes | Yes | `target_commitish` | Source branch/commitish, not necessarily a full SHA |
| `assets_count` | `Count \| None` | Yes | Yes | Length of complete collected `assets` list | Null unless count/completeness is verified |
| `html_url` | `HttpUrl \| None` | Yes | Yes | `html_url` | Observed release page URL |

Draft/unpublished releases with null `published_at` are valid. Missing flags cannot
be replaced with false; publication time cannot be replaced with creation time.
No ordering is imposed between `created_at` and `published_at`, because source
creation timestamps do not necessarily mean publication-object creation.
A known empty assets list yields zero; missing or incompletely collected assets yield null.

GitHub Releases do not include all Git tags and do not equal PyPI package releases.
RepoSeer's package release history must be collected from PyPI, not inferred from
GitHub Releases or tag names.

## 8. GitHub API field mapping

The per-entity tables above are the complete mapping specification for the declared
fields, not executable mappers. Repository IDs come from verified repository context,
never from parsing a URL into an assumed numeric identity. Native numeric IDs must be
checked before explicit decimal-string conversion; the strict entity models accept
neither integer IDs nor string counters. Canonical UTC conversion belongs to the
future extraction adapter, and may never substitute current time for a missing date.

Nested GitHub identities can be missing/null (for example, unlinked commit authors).
Do not fabricate identities, logins, missing counts, parent lists or merge evidence.
Commit detail statistics require actual detail collection; release asset counts require
a complete list. Optional fields that were not collected must be supplied as null.
Native JSON, including omitted keys, source dates and numeric types, remains unchanged
inside `RawRecord.payload`. Entity extraction creates a separate validated object.

### Synthetic illustration

The following complete internal commit entity is invented for illustration; it is not
an observation collected from GitHub. Two known parents establish the merge flag.
Its absent identities, text and statistics remain explicit nulls.

```json
{
  "repo_id": "github:repository:900000001",
  "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "author_github_id": null,
  "author_login": null,
  "committer_github_id": null,
  "committer_login": null,
  "authored_at": "2024-01-02T00:00:00.000Z",
  "committed_at": "2024-01-01T00:00:00.000Z",
  "message": null,
  "parent_shas": [
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "cccccccccccccccccccccccccccccccccccccccc"
  ],
  "is_merge_commit": true,
  "additions_count": null,
  "deletions_count": null,
  "html_url": null
}
```

Additional complete synthetic examples for all five models, including an unpublished
draft release and a known contributor observation, live in
[`test_github_activity_entities.py`](../../tests/unit/schemas/test_github_activity_entities.py).
They are test inputs, not evidence of real collection.

## 9. Identity and validation constraints

| Entity | Logical identity | Cross-field validation |
| --- | --- | --- |
| `GitHubCommit` | `(repo_id, commit_sha)` | Known parents are unique valid SHAs and determine merge flag; unknown parents require unknown flag |
| `GitHubIssue` | Numeric `issue_id` | Known update/close timestamps are at least creation time |
| `GitHubPullRequest` | Numeric `pr_id` | Known update/close/merge timestamps are at least creation time; merge timestamp and true flag require each other, and merge requires closed state |
| `GitHubContributorObservation` | `(repo_id, contributor_github_id, observed_at)` for known users | Anonymous ID/login both null; known users require numeric ID |
| `GitHubRelease` | Numeric `release_id` | Shared type constraints only; no invented creation/publication ordering |

Repository-local issue/PR numbers do not replace stable numeric object IDs. Anonymous
contributors retain raw observation identity rather than a fabricated user ID.
All shared constraints apply even to nullable values when those values are present.
Neither deduplication nor raw/entity provenance validation is implemented here.

## 10. Historical time and leakage rules

The following policies describe the future raw envelope, not additional entity fields:

| Entity | Raw `event_at` policy |
| --- | --- |
| Commit | Valid `committed_at`, otherwise null |
| Issue | `created_at` |
| Pull request | `created_at` |
| Contributor observation | null |
| Release | `published_at` when publication is evidenced; otherwise null |

`event_at` follows source event evidence and does not prove that all payload attributes
were available at that time. `retrieved_at` is when RepoSeer actually obtained the
payload. `available_at` is independently verified historical availability; unknown
means null, never a copy of `event_at` or retrieval time. Canonical `RawRecord` rejects
known event/availability timestamps later than retrieval; these entity schemas have
no raw retrieval field except contributor observation time and cannot perform that
boundary check on their own.

Commit author/committer dates do not establish public push time. Current issue/PR
state, labels, draft status and comment counts are observations, not state at every
cutoff. Contributor aggregates are counts at observation time, not historical counts.
Issue reopen events, PR reviews and maintainer response time require separate
interaction/timeline evidence. Do not infer those histories from a single snapshot.

An event satisfying `event_at <= cutoff_at` does **not** establish cutoff-safe features.
Downstream T-018/T-022 must verify attribute history, availability, coverage and
package-to-repository mapping before using evidence. This phase implements no
historical reconstruction, feature calculation or abandonment labeling.

## 11. RawRecord compatibility and storage conventions

Future activity collectors use the existing T-011 envelope with:

| Key | Required convention |
| --- | --- |
| `schema_version` | `"0.1.0"` |
| `record_type` | `"raw_record"` |
| `source` | `"github_rest"` |
| `source_record_type` | `"repository_activity"` for all five groups |
| `raw_record_id`, `run_id` | Existing canonical raw UUIDv4 and run UUIDv4 formats |
| `repo_id` | Verified canonical numeric GitHub repository identity |
| `package_id` | null unless package mapping is verified; never guess |
| `source_url` | Actual source endpoint URL without credentials |
| `payload` | Original native GitHub JSON object, preserving keys and values |
| `retrieved_at` | Actual canonical UTC retrieval time |
| `event_at` | Per-entity policy in section 10 |
| `available_at` | null unless historical availability is independently verified |
| `raw_body_sha256`, `http_status` | Fill only from actual HTTP metadata/body bytes; otherwise null |

Do not put invented entity fields into native payloads or compute a body hash from
reserialized JSON and describe it as an HTTP body hash. Source-level emails, if
present, must remain governed by the authorized collection scope; this phase neither
fetches them nor adds email fields to entity schemas.

Proposed source-record namespaces and future raw paths:

| Entity | `source_record_id` | Future path |
| --- | --- | --- |
| Commit | `commit:<sha>` | `data/raw/github/commits.jsonl` |
| Issue | `issue:<native-id>` | `data/raw/github/issues.jsonl` |
| Pull request | `pull_request:<native-id>` | `data/raw/github/pull_requests.jsonl` |
| Contributor | `contributor:<native-id>` when present; otherwise null | `data/raw/github/contributors.jsonl` |
| Release | `release:<native-id>` | `data/raw/github/releases.jsonl` |

These are additional T-014 collection conventions, not new T-011 `RecordKind` values.
Entity groups are distinguished by storage paths, source-record namespaces and
collection metadata. Coverage uses existing groups `commits`, `issues`, `pull_requests`,
`contributors` and `releases`; request/result metadata and existing boundary validators
remain applicable. This phase creates none of these JSONL files and implements no
collector, raw adapter, persistence or collection metadata builder.

Canonical `RepositoryActivity` supports a limited event-kind set (commits, issue/PR
transitions and comments), not complete commit/issue/PR attributes, contributor
aggregates or GitHub Releases. Mapping suitable events into it later requires explicit
normalization and source evidence. It is not a superclass or universal representation
of these internal entities; any future public-contract change needs its own versioned
contract decision.

## 12. Scope boundaries for T-014/T-018/T-022

| Phase/task | Responsibility |
| --- | --- |
| T-014 Phase 1 (this change) | Five internal entity schemas, constraints, documentation and offline test definitions |
| Later T-014 collector phase | HTTP collection/pagination, verified repository scope, issue/PR filtering, sufficient anonymous/merge evidence, source timestamp extraction, native raw envelopes and persistence using existing utilities |
| T-018 | Controlled normalization with raw provenance, explicit missingness, event derivation/reconstruction where evidence supports it, and compatible canonical outputs |
| T-022 | Downstream historical/cutoff safety checks and feature use within the agreed package-level modeling scope; no implementation is claimed here |

No CLI changes, legacy DTO replacement, canonical contract/registry changes, generated
artifacts, migrations, dependencies, collectors, raw adapters, storage writers,
feature builders or labels are part of this phase. Existing T-011/T-012/T-013 behavior
is preserved. Unit tests use synthetic in-memory inputs only and require no token,
network or data-file writes.


## 13. Phase 2 — GitHub activity collection implementation

Phase 2 adds `GitHubActivityContractCollector` in
`src/reposeer/collectors/github/activity.py` and `ActivityService` in
`src/reposeer/services/activity_service.py`. The five Phase 1 models and all T-011
contracts remain unchanged. Sections 1–12 describe the schema phase; this section
records the subsequent collection implementation.

### Entry points and verified repository context

`ActivityGroup` is `Literal["commits", "issues", "pull_requests", "contributors", "releases"]`.
Each collector handles one group and one canonical `GitHubRepository` supplied from
the existing T-013 repository collection path. It revalidates that object before any
activity request and requires public, non-fork, non-template, non-mirror metadata.
Archived public upstream repositories remain eligible. Both subject repository ID
and full name must match the verified context (name matching is case-insensitive).
The stable numeric ID comes from that context, never from owner/repo text. A verified
rename changes the endpoint name, not the repository ID. No per-group repository
metadata refetch is performed, and no unverified rename/alias is inferred.

Public signatures:

```text
GitHubActivityContractCollector(group, repository, client=None, *, clock=None)
collector.collect(request)  # -> canonical CollectionResult
collector.close()

ActivityService(client=None, *, storage_paths=None, clock=None)
service.collect_and_store(
    repository, group, *, max_pages=10, max_records=1000, cursor=None, run_id=None
)  # -> canonical CollectionResult, after all emitted records are appended
service.close()
```

An injected GitHubClient belongs to its caller. Collector/service close only clients
they create; the service shares its client with each group collector. A supplied
clock must return aware datetimes; `format_utc_timestamp` formats actual retrieval
and run bounds. It is never used as a fallback for missing historical event dates.

A supported request uses `source="github_rest"`, `record_kinds=["repository_activity"]`,
exactly one subject with non-null canonical `repo_id` and `repo_full_name`, and null
`time_range`/`cutoff_at`. The service supplies null subject `package_id`/`url`; activity
collection does not certify package mapping or use an ancillary URL to resolve identity.
Historical window/cutoff requests
fail with `insufficient_history`; unsupported sources/kinds fail without fetching.
Failed responses echo the request's source so canonical pair validation still passes.
Invalid canonical request objects are rejected by existing boundary validation.

### Native endpoints, extraction and raw envelopes

All requests use existing GitHubClient `list_*` methods and shared T-012 HTTP,
authentication, retry and rate-limit behavior. No additional retry layer is added.
The configured client base URL supplies the endpoint origin/prefix.

| Group | Endpoint suffix | Parameters beyond owner/repo |
| --- | --- | --- |
| `commits` | `/repos/{owner}/{repo}/commits` | `per_page=100`, `page=N` |
| `issues` | `/repos/{owner}/{repo}/issues` | `state=all`, `per_page=100`, `page=N` |
| `pull_requests` | `/repos/{owner}/{repo}/pulls` | `state=all`, `per_page=100`, `page=N` |
| `contributors` | `/repos/{owner}/{repo}/contributors` | `anon=true`, `per_page=100`, `page=N` |
| `releases` | `/repos/{owner}/{repo}/releases` | `per_page=100`, `page=N` |

`list_contributors` adds keyword-only `anon=False`; its existing positional arguments
and default request behavior are preserved. The activity collector opts into `anon=True`.

Five extraction functions instantiate the strict Phase 1 models before raw emission.
Native numeric IDs must be positive integers before decimal-string conversion;
booleans/string IDs are rejected. Counts remain bounded strict integers. Optional
missing/null fields remain null; malformed present values reject that item. Optional
empty/whitespace text may become null in the extracted entity while the native string
remains unchanged in raw. Source timestamps must be aware strings and are explicitly
converted to canonical UTC milliseconds. No emails are extracted into entity models.

Issues containing the `pull_request` key are skipped before validation, including
items with a null value for that key. Skipped PRs are not malformed issues. Known
commit parents determine the merge flag; unknown parents remain null. Commit detail
statistics are not fetched and both statistics fields remain null. PR merge evidence
must be internally consistent; closed alone does not imply merged. Anonymous
contributors require affirmative native `type="Anonymous"` evidence and no account
ID/login; an ambiguous identity is rejected. Their `observed_at` equals raw retrieval.
Release asset arrays are checked for object-list shape, but `assets_count` remains
null because this phase does not certify list completeness or fetch asset details.

Each successful extraction yields an independently validated canonical `RawRecord`:
version 0.1.0, source github_rest, kind repository_activity, fresh raw UUIDv4, request
run ID, verified repository ID and unchanged native payload. No extracted/derived
fields are inserted into the payload. `package_id`, `available_at`, `http_status` and
`raw_body_sha256` are null. Source-record namespaces follow section 11. `source_url`
is the configured group endpoint without credentials or query parameters.

One actual retrieval timestamp is captured immediately after obtaining each page and
shared by that page's records and contributor observations. Commit `event_at` uses
validated committed time; issues/PRs use creation time; contributors use null;
releases use published time only for non-draft releases with publication evidence.
Canonical raw validation rejects future event times; dates are not moved or filled
to make validation pass. Source creation/publication dates do not certify historical
availability, state at a cutoff, public commit push time or PyPI release history.

### Pagination and continuation cursors

All groups share one synchronous pagination loop with fixed page size 100.
`max_pages` limits list-page fetches per call; `max_records` limits valid emitted
raw records. Invalid items and skipped PRs consume native positions but not the
record budget. Unexpected non-list or oversized page responses are errors, never
empty-page success. `pages_fetched` counts successfully obtained valid list pages,
including empty pages; HTTP/JSON/invalid page-shape failures do not add to this count.

The cursor format is exactly `p:<page>:i:<index>`: positive 1-based page and index
0–99 of the next unprocessed native item. `cursor=None` starts at page 1, index 0.
Examples are `p:1:i:0` and `p:3:i:27`. Malformed cursors fail before fetching.
A starting offset beyond the returned page yields an explicit error rather than
silently certifying completion.

- An empty page or a fully consumed page shorter than 100 proves pagination complete;
  `next_cursor` is null. This includes an exact record-budget match at the end of a
  short page.
- Stopping inside a page yields partial coverage and the next native index.
- Consuming a full page while exhausting either budget yields partial coverage and
  cursor `p:<next page>:i:0`; no extra fetch exceeds the budget to prove exhaustion.
- An API failure retains earlier emitted records and points to the failed page's
  starting offset. Valid items on a page survive other items' validation failures.

Cursors are scoped to the same repository and group and can be supplied to a later
explicit call. They do not encode or verify that scope themselves, freeze upstream
ordering, guarantee stable pagination, or survive crashes automatically. A caller
must retain the scope and use the continuation intentionally.

### Results, errors and raw persistence

The collector returns T-011 `CollectionResult` with request/run echoes, canonical
metadata, emitted/rejected counts, run bounds, pagination status and one `Coverage`
for the selected group. Collector name echoes the request; collector version is
`SCHEMA_VERSION`. Unknown rate-limit metadata remains null. Every returned result
passes `validate_collection_pair`.

| Status | Meaning | Coverage |
| --- | --- | --- |
| `complete` | Proven endpoint exhaustion, no errors/rejections; zero records can be complete | complete, no missing reason |
| `partial` | At least one valid list page fetched with errors or incomplete pagination/budgets; may emit zero records | partial, explicit parse_error/unavailable/not_collected reason |
| `failed` | No valid list page fetched and a blocking scope, request, HTTP, JSON or page-shape error | unavailable with explicit reason and at least one error |

Coverage `records_observed` counts emitted validated records, not skipped PRs or
rejected items. A malformed item creates a safe `validation_error` or `parse_error`,
with a source-record ID only when safely known and null raw-record ID. No provider
body, token or validation input is included in diagnostics. HTTP errors map 404 to
`not_found`, rate limits to `rate_limited`, network errors to `network_error`, successful
HTTP responses containing malformed JSON to `parse_error`, and other statuses to
`http_error`. Retryable flags follow existing transient HTTP/rate-limit/network
semantics; the collector does not retry independently.

`ActivityService` builds one canonical request with collector name
`github_activity_<group>`, fresh request UUIDv4 and either the caller's validated run
UUIDv4 or a new one. After collection and pair validation it appends every returned
raw record through `JsonlStorage.append_contract_record` and
`StoragePaths.raw_path("github", group)`:

- `data/raw/github/commits.jsonl`
- `data/raw/github/issues.jsonl`
- `data/raw/github/pull_requests.jsonl`
- `data/raw/github/contributors.jsonl`
- `data/raw/github/releases.jsonl`

Partial results' valid records are persisted too. Failed/empty results do not create
a raw dataset. OSError/StorageError during append becomes a clear `CollectionError`;
the service returns no success report. Previous observations and already successful
appends remain intact. New collection runs may create new observations of the same
upstream entity; there is no global raw deduplication or exactly-once guarantee.

### Remaining Phase 3 work

Phase 2 has no durable checkpoints, automatic resume or page transaction/idempotency
protocol. A returned fetch cursor is not proof that raw storage succeeded. Phase 3
must connect durable checkpoint advancement to successful persistence, implement
multi-repository orchestration and perform separately authorized live verification.
Full interaction/timeline/review data, processed activity Parquet, T-018 normalization,
historical features and labels remain outside this implementation. Existing CLI,
legacy collectors, entity models, T-011 contracts and T-013 services are unchanged.
T-014 as a whole is not declared complete by this phase.


## 14. Phase 3 — Durable activity collection and batch execution

### Architecture and public interfaces

`ActivityBatchService` performs synchronous, sequential repository/group execution.
It shares one `GitHubClient` with T-013 `RepositoryService` and `ActivityService`.
Injected clients belong to the caller; an internally created client is closed once
by `ActivityBatchService.close()`. Importing the CLI performs no collection or HTTP
request. The original `reposeer collect` and Phase 2 `collect_and_store()` APIs remain
unchanged.

```python
ActivityBatchService(client=None, *, storage_paths=None, clock=None,
                     run_manager=None, on_run_started=None)
service.start(targets, *, groups, max_pages_per_group=5,
              max_records_per_group=500) -> ActivityBatchResult
service.resume(run_id) -> ActivityBatchResult
service.close()
```

`on_run_started(run_id, checkpoint_path)` runs after the initial durable checkpoint
and before metadata collection. The CLI uses it to display the resumable run ID
before potentially lengthy work. An injected `RunManager` must use the supplied
`StoragePaths.metadata_dir / "collection_runs"` directory.

`ActivityService.collect_page_and_store(repository, group, *, context, run_manager,
target_index, cursor, remaining_record_budget)` builds a canonical T-011 request and
calls the existing `GitHubActivityContractCollector` with `max_pages=1`. It does not
implement another pagination loop or replace the five extractors. A collection step
is bounded to one native page (100 items), possibly ending inside the page when the
remaining record budget is consumed. Native PR skips and rejected items still affect
the Phase 2 `p:<page>:i:<index>` cursor. Empty successful pages can prove exhaustion;
full pages cannot. Phase 1 models, all Phase 2 native mappings, time rules and schema
version `0.1.0` are preserved.

### Initial manifest and checkpoint state

The sole authoritative mutable run state remains the T-012 checkpoint:

```text
data/metadata/collection_runs/run_<uuidv4>.json
```

`RunManager.start_run(initial_checkpoint_state=...)` validates keys/primitive values
through `CollectionContext.set_checkpoint`, includes them in its first atomic write,
and refuses to overwrite existing runs. Older positional calls remain compatible.
`CollectionProgress` remains fetch accounting and is not used as a storage transaction.

`activity:manifest` is a JSON string with exactly:

```json
{
  "manifest_version": 1,
  "schema_version": "0.1.0",
  "targets": ["Synthetic/Demo"],
  "groups": ["commits", "issues", "pull_requests", "contributors", "releases"],
  "max_pages_per_group": 5,
  "max_records_per_group": 500
}
```

Targets use T-013 `parse_repository_target()`, preserve first appearance/casing and
remove case-insensitive duplicate slugs/URLs. Inputs must contain 1-1,000 targets;
invalid targets fail before run creation. Groups are a unique nonempty subset;
`all` expands to the order shown above. Page budgets are strict integers in
1-10,000; record budgets are strict integers in 1-1,000,000. Run IDs use canonical
lowercase UUIDv4. Resume takes no target/group/budget overrides and explicitly
revalidates the manifest. `activity:manifest_sha256` detects inconsistent manifest
edits; it is an integrity check, not an authentication mechanism.
`activity:started_at` stores the injected clock's canonical UTC run start.

Repository keys are `activity:repo:<target_index>:status`, `:error_code`, and, after
successful verification, `:context` containing canonical repository JSON. Per-group
keys use `activity:item:<target_index>:<group>:` with these fields:

| Field | Initial value | Meaning |
| --- | --- | --- |
| status | pending | pending, complete, partial, retryable_failure, failed or excluded |
| cursor | empty string | Next unprocessed native position; empty becomes request cursor null |
| pages_fetched | 0 | Cumulative successful list pages |
| records_emitted | 0 | Cumulative validated emissions, including logical duplicates |
| records_stored | 0 | Distinct persisted raw observations |
| records_rejected | 0 | Cumulative native validation failures |
| error_count | 0 | Cumulative canonical collection errors |
| pagination_complete | 0 | Integer 0/1, proven endpoint exhaustion |
| last_error_code | empty string | Last canonical error code |
| last_request_id | empty string | Last committed request, used to recognize stale journals |
| missing_reason | empty string | Explicit budget, rejection, collection error or exclusion reason |

Every checkpoint value remains a string/integer, never a nested object, boolean or
serialized Python object. Run-level collected/error counters agree with committed
per-group counters. The manifest remains unchanged throughout execution.

### Verified repository identity and eligibility

Each new target calls public T-013 `RepositoryService.collect_and_store(owner, repo)`
once before any activity. Its returned canonical repository is revalidated, screened
for public/non-fork/non-template/non-mirror eligibility and checkpointed. Archived
public repositories remain eligible. T-013 communicates verified exclusions through
fixed `CollectionError` diagnostics; the batch recognizes those diagnostics without
changing RepositoryService or duplicating its normalization/public-scope logic.
Repository metadata retains T-013 raw/processed persistence behavior.

Resume restores and validates existing contexts, identities, provenance endpoints
and manifest bindings without refetching metadata. Unverified/failed metadata can
be retried on an explicit resume. Invalid stored context fails closed before requests.
Different input slugs resolving to an already processed numeric `repo_id` are reported
as duplicates and do not collect activity again. Numeric identity, not the slug, is
stable across names; supplied names must still pass the existing T-013 verification.
A previously failed target resolving on resume to an already processed identity is
also skipped. No package-to-repository mapping is invented.

### Journal lifecycle and commit order

A bounded step uses this temporary write-ahead journal:

```text
data/metadata/collection_runs/activity_<uuidv4>_<numeric_repo_id>_<group>_pending.json
```

Journal names derive only from verified numeric repository IDs, canonical run UUIDs
and supported group literals. A journal contains version 1, run/repository/group/
target identities, nullable start cursor, canonical request and result, and
`state="pending"`. `before_state` captures the original group checkpoint fields;
`append_raw_ids` freezes the original decision about which emitted UUIDs to persist
when known native duplicates were skipped. Full staged RawRecord envelopes retain
native payloads, timestamps and original UUIDs. Zero-record results are journaled
as well. Existing pending journals cannot be silently overwritten.

The successful order is:

1. Fetch one bounded step and validate the canonical request/result pair.
2. Atomically persist its pending journal through `write_json_atomic()`.
3. Append only missing staged records to the existing group JSONL.
4. Flush and `os.fsync()` JSONL before returning from durable append.
5. Apply staged counters/cursor and atomically save the RunManager checkpoint.
6. Remove the pending journal before fetching the next step.

Raw paths remain `data/raw/github/{commits,issues,pull_requests,contributors,releases}.jsonl`.
`JsonlStorage.append_contract_records_durable()` validates the bounded batch before
writing, appends one canonical envelope per line, fsyncs and returns appended count.
All prior storage methods retain their existing semantics.

Append failure leaves the previous checkpoint and journal available. Checkpoint
failure leaves the journal and rolls back in-memory progress. Deletion failure after
checkpoint success retains the committed checkpoint; resume recognizes the original
request and removes the stale journal without appending or counting it again. A
failed initial journal write leaves progress unchanged and requires a fresh fetch.
No report claims such a step committed.

### Resume, replay and deduplication

Resume checks all pending journal filenames/scopes before new API calls. It validates
journal structure/version/identity, original contracts, native identities, group
endpoints, cursor/configuration, original checkpoint snapshot and append selection.
Conflicts fail closed. A journal must match either the checkpoint before its step or
the exact checkpoint after that same request. Anything else is inconsistent.

Recovery streams only the necessary group JSONL and builds reusable UUID/digest and
logical identity indexes. Native identity/time indexes retain only the current run,
with per-repository UUID sets for bounded reconciliation/report lookups; other runs
retain UUID digests for collision checks. It does not retain another copy of native
payloads or rescan all files after every page. Existing identical UUID/content pairs
are skipped; the same UUID with different canonical content is an integrity error.
Missing staged records reuse their original UUIDs and timestamps. Any appended data
is fsynced before counters are applied exactly once.

Known native deduplication uses `(run_id, repo_id, group, source_record_id)`; group is
implicit in its separate dataset/index. It suppresses repeats on moving upstream
pages within one run. Different runs represent new observations and are not merged.
Anonymous contributors have no invented native identity: replay is idempotent by
original raw UUID, but separate identical anonymous observations remain separate.
This is not a global exactly-once guarantee.

Invalid JSON, invalid canonical/raw group identity, conflicting UUID content, or an
incomplete final JSONL line prevents automatic recovery and identifies the affected
file. Raw files are never automatically truncated, filtered, replaced or rewritten.
A partial final line needs explicit manual repair before resume.

The protocol supports **one active writer per run**. Concurrent writers to the same
run are unsupported. Filesystem corruption and changing provider pagination cannot
be solved solely through checkpoint replay. Valid raw files and valid journals are
prerequisites for automatic recovery; there is no distributed transaction or locking
system in this phase.

### Budgets, failures and batch coverage

Budgets apply cumulatively to emitted records and successfully fetched pages across
the entire group run, including resume. Storage deduplication does not refund the
record budget. An HTTP failure with no successful page leaves cursor/page counters
unchanged. Explicit resume can retry `retryable_failure`; completed/excluded/failed
groups and terminal partial groups are skipped. A fixed budget cannot be increased
by resume; start a new run for larger budgets.

`complete` requires proven pagination exhaustion and no cumulative rejection/error.
`partial` distinguishes `budget_exhausted`, `native_rejections` and
`collection_errors`. Earlier API errors remain in cumulative counts even when a
subsequent resume succeeds, so exhaustion after such a retry can still be partial.
Retryable activity failures stop that group for this execution. Metadata and activity
failures are isolated so later eligible targets/groups can proceed. Invalid recovery
state stops unsafe resume; persistence interruptions retain recovery information.

Batch `complete` requires all eligible unique requested groups complete, with no
unresolved failure/exclusion/pending cleanup. Skipped identity duplicates alone do
not invalidate completeness. A completed group or persisted usable records alongside
incomplete coverage produces `partial`; absence of either produces `failed`.
All-excluded batches are failed. Pending persistence/metadata/API work is resumable;
budget-limited partial coverage cannot be bypassed.

### CLI and machine-readable report

```bat
reposeer collect-activity start --repos-file repos.txt --groups all --max-pages 5 --max-records 500
reposeer collect-activity start --repos-file repos.txt --groups commits,issues,pull_requests --data-dir data_pilot
reposeer collect-activity resume --run-id <uuidv4> --data-dir data_pilot
```

These are documentation examples, not commands executed during implementation.
The UTF-8 target file contains one slug/HTTPS GitHub URL per line; blank lines and
left-trimmed comment lines beginning with `#` are ignored. Unknown/duplicate/empty
groups, invalid targets/budgets and missing/empty files are rejected before collection.
Resume has only run ID/data directory options. Rich output shows run ID/status,
unique requested targets, repository outcomes, per-group pages/stored/rejected/
coverage, resumability and checkpoint/report paths. Diagnostics do not print tokens,
provider bodies or arbitrary raw content. Exit codes: 0 complete, 1 incomplete or
recovery/persistence problem, 2 invalid CLI input/configuration where practical.

The atomic report is:

```text
data/metadata/collection_runs/activity_<uuidv4>_report.json
```

It uses top-level `report_version`, `run_id`, `schema_version`, `status`, `started_at`,
`finished_at`, `targets_requested`, repository processed/excluded/duplicate/failed
counts, `total_raw_records_stored`, `groups` and `limitations`.
Every group includes repo ID/full name, group/status, pages fetched, records emitted/
stored/rejected, error count, pagination completeness, nullable next cursor/missing
reason, min/max event time and null event/availability counts. Repo ID is null when
T-013 never returned a verified context. Duplicate target groups are reported as
excluded from repeated work and contribute no additional observations.

Counts derive from the durable checkpoint and validated stored observations.
Uncommitted journal records are excluded even if their raw append already reached
disk. Event metrics select only committed observations for this run/repository/group.
Missing times remain null; zero records do not imply that no source events existed.
Report write failure raises an explicit error; a valid report can be regenerated
through resume, without refetching completed groups.

### Historical limitations and remaining manual verification

REST pages are not a frozen historical snapshot. Pagination completeness does not
certify reconstruction for 2019-2025 or any prediction cutoff. `min_event_at` is not
the beginning of certified coverage. Current issue/PR/contributor states cannot be
backfilled to every cutoff, contributor aggregates are not historical contributor
counts, and `available_at=None` remains unknown. GitHub Releases are not PyPI package
releases. No feature, abandonment label, normalization dataset, EDA, profiling or
integrated T-020 pipeline is produced here.

Runtime tests and Ruff have deliberately not been executed by Codex; only static
source inspection was performed. After manually passing focused recovery tests,
Phase 1-2/T-011-T-013 regression
checks, the non-smoke suite and Ruff in Windows CMD, separately perform authorized
live verification: a small public-repository/subset run in a separate data directory,
check raw/report/checkpoint consistency, interrupt/resume with the same UUID, verify
metadata reuse and cumulative budgets, and inspect real rate-limit behavior. Do not
use live smoke collection as a substitute for the offline durability tests.
