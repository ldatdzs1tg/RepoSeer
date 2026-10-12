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
