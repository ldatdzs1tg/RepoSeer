# RepoSeer Data Contract v0.1

**Schema version:** `0.1.0`  
**Ngày lập:** 08/10/2026  
**Áp dụng:** collector, normalizer, technology linker và snapshot builder của demo RepoSeer.

Tài liệu này chốt syntax và interface trước khi các thành viên code song song. Mọi module trao đổi JSON theo các định nghĩa trong `shared/models.py`; JSON Schema, TypeScript types và bảng field được sinh từ cùng định nghĩa đó. Không tự đặt alias hoặc thay đổi tên trường ở module riêng.

Phạm vi nền là Repository Scope v0.1: package Python trên PyPI, repo nguồn GitHub public/non-fork, lịch sử 2019–2025 và snapshot theo quý 2020–2024. Technology và article là dữ liệu bổ trợ; contract không biến một keyword hoặc một bài viết thành nhãn abandoned tự động.

## 1 Nguồn định nghĩa gốc và cấu trúc file

| File | Vai trò | Cách thay đổi |
| --- | --- | --- |
| `shared/models.py` | Python/Pydantic models, constraints, cross-field checks, Protocol interfaces | Sửa tại đây trước |
| `shared/identity.py` | Chuẩn hóa ID, URL, timestamp và content hash | Thay đổi cùng contract khi ảnh hưởng semantics |
| `shared/validation.py` | Validate record và đối chiếu request/result | Chạy tại biên module |
| `shared/schemas/data_contract.schema.json` | JSON Schema Draft 2020-12 của toàn bộ contract | Tự sinh, không sửa tay |
| `shared/types.ts` | TypeScript types và interface Promise | Tự sinh, không sửa tay |
| `docs/schema_reference.md` | Danh sách đầy đủ field/type/bắt buộc | Tự sinh, không sửa tay |
| `examples/*.json` | Record và các cặp I/O giả lập đã validate | Tạo lại bằng script hoặc thêm fixture có kiểm tra |

`record_type` phân biệt các loại record; `schema_version` phải bằng `0.1.0`. Một object đúng schema chưa chắc đạt tiêu chí chọn repo. Public/non-fork là bước eligibility riêng; schema vẫn cho phép biểu diễn repo bị loại để lưu lý do và kiểm tra pipeline.

## 2 Naming convention và kiểu dữ liệu

| Thành phần | Quy tắc | Ví dụ |
| --- | --- | --- |
| JSON field, biến và hàm Python | `snake_case` | `repo_id`, `published_at`, `raw_record_ids` |
| Class/model/interface | `PascalCase` | `GitHubRepository`, `TechnologyEntity`, `CollectionResult` |
| Enum value và `record_type` | `lower_snake_case`, đúng literal | `historical_reconstruction`, `technology_entity` |
| Hằng số code | `UPPER_SNAKE_CASE` | `SCHEMA_VERSION` |
| Boolean canonical | Có nghĩa rõ, thường dùng `is_` | `is_fork`, `is_archived` |
| Counter | Integer không âm, tối đa `2^53 − 1` | `commit_count_12m` |
| Số có đơn vị | Ghi đơn vị vào tên | `bytes_count`, `package_age_days` |
| Timestamp | Hậu tố `_at` | `cutoff_at`, `retrieved_at` |
| Ngày thiếu giờ | Hậu tố `_date` | `published_date` |
| Danh sách ID | Hậu tố `_ids` | `repo_ids`, `package_ids` |

Không chấp nhận `repoId`, `repoID`, `repository_name` thay cho `repo_full_name`, hoặc string `"false"` thay cho boolean `false`. ID nhà cung cấp được chuẩn hóa thành string; version package giữ nguyên chuỗi từ nguồn, không giả định mọi version là SemVer. `NaN`, `Infinity`, `undefined` và số âm trong counter bị từ chối.

Canonical object dùng `extra="forbid"`. Thêm field lạ hoặc bỏ một key bắt buộc đều là lỗi. Chỉ `payload` của raw record cho phép key theo syntax gốc của nhà cung cấp. Counter phải được serialize là số nguyên; TypeScript `number` không thay thế kiểm tra runtime.

## 3 Repository identity và các ID khác

| Entity | ID canonical | Quy tắc |
| --- | --- | --- |
| Repository | `github:repository:900000001` | Dùng GitHub numeric repository ID, biểu diễn decimal string |
| Package | `pypi:demo-maintenance-package` | Lowercase; gom liên tiếp `-`, `_`, `.` thành một `-` |
| Technology | `tech:python` | Slug lowercase kebab-case của concept, không chứa version |
| Article | `article:sha256:<64 hex>` | SHA-256 của URL canonical, UTF-8 |
| Raw observation | `raw:<UUID v4 lowercase>` | Một lần quan sát mới; giữ ID khi replay record đã lưu |
| Request/run | UUID v4 lowercase | `request_id` cho một lời gọi; `run_id` cho toàn bộ pipeline/job |
| Repository activity | `github:activity:<repo numeric ID>:<activity_kind>:<event ID>` | Commit dùng SHA; timeline/comment dùng ID sự kiện ổn định |

`RepoIdentity` bắt buộc chứa `repo_id`, `github_repo_id`, `github_node_id`, `repo_full_name`, `repo_url`, `known_urls`. `github_node_id` có thể null; numeric ID không được null. `repo_id` phải khớp `github_repo_id`. `repo_full_name` giữ giá trị owner/name quan sát được, còn `repo_url` là `https://github.com/<owner>/<name>` lowercase, không có `.git` của clone URL, query, fragment hoặc slash cuối.

Khi repo chuyển owner/đổi tên, giữ `repo_id`; cập nhật full name/URL của observation mới và giữ URL cũ trong lịch sử hoặc `known_urls`. Không dùng URL hoặc owner/name làm khóa join ổn định. Nhiều package trong một repo vẫn có package ID riêng và cùng `repo_group_id` để chia tập. Monorepo chưa xác minh mapping được giữ pending; không đoán package path.

ID package được tạo bằng `package_id(name)` trong `shared/identity.py`. Không tự suy diễn ID technology từ tên package: Python là một technology, còn package PyPI là một distribution. Alias technology phải qua registry; `tech:python` và `pypi:python` không thay thế nhau.

JavaScript collector cần đọc ID lớn mà không làm tròn. Nếu native JSON có ID vượt giới hạn số nguyên an toàn, dùng cách đọc lossless hoặc giữ bytes gốc để adapter xử lý; chuyển một `number` đã bị làm tròn sang string không khôi phục được ID.

## 4 Timestamp và timezone

Canonical timestamp có đúng dạng `YYYY-MM-DDTHH:mm:ss.sssZ`, ví dụ `2026-10-08T08:00:00.000Z`. Mọi giá trị là UTC, có ba chữ số millisecond. Adapter chuyển offset nguồn sang UTC và cắt độ chính xác dưới millisecond; boundary validator không tự chuyển kiểu. Timestamp thiếu timezone bị từ chối. Chỉ UI mới chuyển sang múi giờ Asia/Saigon.

| Trường | Ý nghĩa |
| --- | --- |
| `retrieved_at` | Thời điểm thực sự lấy dữ liệu từ nguồn; không dùng thời điểm chạy normalizer thay thế |
| `event_at` | Thời điểm commit/timeline event thực sự xảy ra theo bằng chứng nguồn |
| `valid_at` | Mốc mà observation hoặc trạng thái tái dựng mô tả |
| `available_at` | Mốc đã xác minh thông tin có thể được biết; unknown thì null |
| `cutoff_at` | Mốc cuối thông tin được phép dùng cho feature |
| `horizon_end_at` | `cutoff_at` cộng 12 tháng lịch; ngày không tồn tại được clamp về cuối tháng |
| `published_at` | Thời điểm xuất bản có timezone và độ chính xác giờ/phút/giây từ nguồn |
| `published_date` | Ngày xuất bản khi nguồn chỉ cho ngày; không giả tạo timestamp 00:00 |
| `content_available_at` | Mốc đã xác minh đúng bản nội dung article này có thể được biết |
| `yanked_status_at` | Mốc mà trạng thái yanked được quan sát hoặc xác minh |

Lưu ý chuyển tên: `cutoff_date` và `horizon_end` trong mô tả Scope là khái niệm; tên field canonical trong code là `cutoff_at` và `horizon_end_at`. Không gửi cả hai phiên bản key.

Time range lịch sử dùng `(start_at, end_at]`: bỏ mốc bắt đầu, lấy đến hết mốc kết thúc. `start_at < end_at`. Với snapshot cuối quý, cutoff là cuối ngày UTC, ví dụ `2023-06-30T23:59:59.999Z`. Một object repository được lấy năm 2026 không trở thành trạng thái năm 2023 chỉ bằng cách đổi `valid_at`.

`temporal_basis="observation"` yêu cầu `valid_at == retrieved_at`. `historical_reconstruction` cho phép `valid_at` cũ hơn, nhưng phải có phương pháp tái dựng và raw evidence. `available_at`, `valid_at` không được sau `retrieved_at`. Nhãn dùng sự kiện sau cutoff phải được xử lý ở bước riêng; không đưa vào feature request.

## 5 Null và dữ liệu thiếu

- Mọi key khai báo trong model đều phải xuất hiện. Kiểu `T | None` nghĩa là key có thể có giá trị JSON `null`, không phải được bỏ key.
- `null` nghĩa là không biết/không thu/không áp dụng theo context. Ghi lý do trong `provenance.missing_fields`, coverage hoặc trường precision/status tương ứng.
- `0`, `false` và `[]` chỉ dùng khi đã xác minh giá trị hoặc danh sách rỗng. Không biến timeout, lỗi API hay chưa gọi endpoint thành giá trị rỗng.
- `authors=null` là không xác định tác giả; `authors=[]` là đã xác định nguồn không có tác giả. `language_bytes=null` là chưa có dữ liệu, khác `[]` đã xác minh không có mục ngôn ngữ.
- `dependencies=null` là thiếu metadata; `dependencies=[]` là metadata xác nhận không khai báo dependency.
- Nullable text không dùng chuỗi rỗng. Ngoại lệ: `version_specifier=""` biểu thị dependency không có constraint.
- Với `PackageSnapshot`, mọi feature null bắt buộc có đúng một `missing_fields` entry ở path `features.<field>`. Không ghi lý do cho feature đã biết hoặc field không tồn tại.

Lý do dùng chung: `not_in_source`, `not_collected`, `unavailable`, `parse_error`, `not_applicable`, `ambiguous`, `historical_unavailable`. Các object `MissingField` dùng `field_path`, `reason`, `detail`; `detail` cũng phải xuất hiện dù null.

## 6 Raw record envelope

Envelope tách metadata thu thập khỏi payload native. Raw record là observation được lưu để replay/audit; normalizer tạo object mới và không đổi key hoặc nội dung raw payload.

| Nhóm | Field |
| --- | --- |
| Phiên bản và loại | `schema_version`, `record_type="raw_record"` |
| Định danh observation | `raw_record_id`, `run_id`, `source_record_id` |
| Nguồn và loại native | `source`, `source_record_type`, `source_url` |
| Subject đã xác minh | `repo_id`, `package_id` |
| Thời gian | `retrieved_at`, `event_at`, `available_at` |
| Tình trạng phản hồi | `http_status`, `raw_body_sha256` |
| Payload native | `payload`, object chứa JSON values |

`source_record_id` là string ID từ provider khi xác định được; dùng null nếu không có. `event_at=null` với response repository vì đây là trạng thái, không phải một sự kiện archive. Request lỗi không được ngụy trang thành payload rỗng thành công: trả `DataError` trong result. Chỉ emit raw record khi payload có ích đã đọc được; phản hồi lỗi thuộc errors/logging.

`raw_body_sha256` là SHA-256 bytes của HTTP body sau HTTP content decoding, trước khi parse JSON/HTML. Không hash JSON được serialize lại rồi gọi là hash bytes gốc. Trường này null nếu không có bytes để xác minh. Hash content article là quy tắc khác, nêu ở phần 8.

Danh sách `source`: `github_rest`, `github_graphql`, `pypi_json`, `pypi_index`, `pypi_bigquery`, `gh_archive`, `deps_dev`, `osv`, `project_docs`, `web_article`, `source_archive`, `technology_catalog`.

`source_record_type` có thể là repository, release, activity, dependency metadata, article, technology entity hoặc security advisory theo enum trong code. Payload của một loại chưa có adapter vẫn được lưu raw. Normalizer phải trả `unsupported_record_type` khi chưa hỗ trợ, không drop im lặng. Bộ normalized models v0.1 bàn giao gồm repository, release có dependency requirements, activity, article và technology; graph-resolved/advisory riêng cần bổ sung model trước khi xuất normalized.

Payload native giữ `full_name`, `archived`, `open_issues_count` của GitHub. Normalized repository dùng `identity.repo_full_name`, `is_archived`, `open_issues_and_prs_count` để nói rõ semantics; không coi count tổng này là số issue riêng. Primary language không đủ để kết luận repo ngoài phạm vi package Python.

## 7 Technology schema

`TechnologyEntity` mô tả concept, không phải một version/release. Các nhóm field gồm identity (`technology_id`, `slug`, `name`), phân loại (`category`, `aliases`), mô tả (`description`, `official_urls`), liên kết (`repo_ids`, `package_ids`), lifecycle (`lifecycle_status`, `status_evidence_url`) và `provenance`.

Category dùng `language`, `framework`, `library`, `runtime`, `database`, `developer_tool`, `platform`, `standard`. Technology ID phải bằng `tech:<slug>`. Aliases không được trùng theo case-insensitive; ID/URL lists không được trùng. Registry thống nhất slug trước khi linker dùng. Khi đổi cách đặt slug, có migration/alias mapping; không tự đổi ID đã phát hành.

Lifecycle gồm `active`, `deprecated`, `end_of_life`, `unknown`. Deprecated/EOL bắt buộc có URL bằng chứng. Lifecycle của một technology không phải nhãn abandoned của package; package chứa alias trong README cũng không tự được gán nhãn.

`TechnologyMention` trong article gồm `technology_id`, `matched_text`, `confidence`, `match_method`, `extractor_version`. Confidence nằm trong `[0,1]`, là mức tin cậy của việc nhận diện technology, không phải xác suất abandoned. `match_method` là `dictionary`, `manual` hoặc `model`.

## 8 Article schema

`Article` áp dụng cho bài viết, announcement, documentation, changelog và tin bảo mật trong các nguồn được chọn. Schema giữ URL, tiêu đề, tác giả, ngôn ngữ, publication precision, nội dung, liên kết technology/package/repo và provenance. Bảng field đầy đủ nằm trong [schema_reference.md](schema_reference.md#article).

### Identity và URL

Ưu tiên canonical URL được scraper xác minh, hoặc final URL sau redirect; sau đó chạy `canonical_article_url`. Quy tắc chung: lowercase scheme/hostname, IDNA hostname, bỏ default port và fragment; bỏ `utm_*`, `fbclid`, `gclid`; chuẩn hóa query encoding và giữ thứ tự query có ý nghĩa. Không tự đổi HTTP thành HTTPS, đổi case path hoặc gộp đường dẫn có/không slash cuối. URL chứa credentials/whitespace bị từ chối.

`article_id = "article:sha256:" + SHA256(canonical_url UTF-8)`. URL gốc giữ trong `original_url`. Article ID là khóa tài liệu theo URL; nội dung thay đổi được lưu bằng observation mới và hash mới, không tự ghi đè bản lịch sử.

### Publication và nội dung

| `published_precision` | Quy tắc |
| --- | --- |
| `timestamp` | Có `published_at`; `published_date` bằng phần ngày UTC của timestamp |
| `date` | Có `published_date`; `published_at=null`; giữ ngày nguồn cung cấp |
| `unknown` | Cả `published_at` và `published_date` đều null |

`content_status` là `full`, `snippet` hoặc `unavailable`. Full/snippet cần `content_text` và `content_sha256`; unavailable yêu cầu text/hash/content availability đều null. Text đã tách khỏi HTML được chuẩn hóa Unicode NFC, newline LF và bỏ whitespace ngoài cùng. Không lower-case hoặc gộp khoảng trắng bên trong. Hash là SHA-256 UTF-8 của đúng text chuẩn hóa đó; validator kiểm tra hash khớp.

Published date không chứng minh bản text hiện tại đã tồn tại vào ngày đó. `content_available_at` chỉ được điền khi có phiên bản/bản lưu có mốc xác minh; nếu không, để null. Việc tìm thấy “deprecated” phải được đọc theo ngữ cảnh và kiểm tra package, người công bố, ngày và việc cessation thực sự xảy ra. Schema article không có field `label` hoặc `abandonment_probability`.

## 9 Collection metadata và lỗi

`CollectionRequest` gồm request/run ID, tên collector, source, subjects, record kinds, time range/cutoff, cursor và giới hạn pages/records. Subject cần ít nhất một repo ID, full name, package ID hoặc URL. Khi đủ cả ID và name, collector phải resolve/đối chiếu với provider; identity không khớp thì báo lỗi, không ghi đè ID.

`CollectionResult` echo `request_id`, `run_id`, `source`; chứa `records`, `errors`, `status`, `metadata`. Metadata gồm collector name/version, started/finished times, requested range, pages/counts, pagination, next cursor, coverage và rate-limit info.

| Tình trạng | Ý nghĩa |
| --- | --- |
| `complete` | Thu xong phạm vi đã yêu cầu; không errors/rejections; pagination hoàn tất; coverage không partial/unavailable |
| `partial` | Có dữ liệu hoặc phần xử lý thành công nhưng có lý do rõ: page budget, lỗi, rejected record hoặc coverage thiếu |
| `failed` | Không emit record; ít nhất một error |

`records_emitted == len(records)`. `started_at <= retrieved_at <= finished_at` cho từng raw record. Raw record trong result phải cùng run/source. Pagination complete yêu cầu `next_cursor=null`; pagination chưa xong dùng cursor nếu provider có, hoặc null kèm reason khi không thể resume. Collector không vượt `max_pages` hoặc `max_records`.

Mỗi result có ít nhất một `Coverage`. Data group dùng enum trong code; coverage gồm `status`, `time_range`, `records_observed`, `missing_reason`. Complete + zero records nghĩa là đã quan sát và không tìm thấy hoạt động. Partial/unavailable/not-applicable phải có lý do. Unavailable/not-applicable không báo số record quan sát lớn hơn 0.

`DataError` gồm `code`, `message`, `retryable`, `source_record_id`, `raw_record_id`. Không đưa token, Authorization header hoặc credential vào URL/error message/payload envelope. Raw body chứa dữ liệu nguồn chỉ được lưu trong phạm vi dữ liệu đã cho phép; không thu danh sách quyền private hoặc email cá nhân bổ sung.

## 10 Interface giữa các module

| Module | Input | Output | Trách nhiệm |
| --- | --- | --- | --- |
| GitHub/PyPI/article/technology collector | `CollectionRequest` | `CollectionResult` | Lấy dữ liệu, giữ raw payload, ghi provenance/coverage và lỗi |
| Normalizer | `NormalizationRequest` | `NormalizationResult` | Chuyển native keys sang canonical models; không đặt lại retrieved time |
| Technology linker | `TechnologyLinkRequest` | `TechnologyLinkResult` | Nhận articles + taxonomy; chỉ cập nhật `technology_mentions` |
| Snapshot builder | `SnapshotBuildRequest` | `SnapshotBuildResult` | Nhận release/activity đã lọc đúng thời gian; xuất feature snapshot và missing reasons |

Python dùng các Protocol trong `shared/models.py`; TypeScript dùng Promise interfaces trong `shared/types.ts`. Đây là interface dữ liệu, không phải implementation API/collector. Một `run_id` xuyên suốt pipeline; mỗi lời gọi có `request_id` mới, result echo đúng request. Khi retry nguyên request có thể giữ request ID; re-collect observation mới dùng raw UUID mới.

Normalizer có thể tạo nhiều output từ một raw record. `processed_record_count` đếm raw input được xử lý, `output_record_count` đếm normalized outputs. `input_record_count = processed_record_count + rejected_record_count`. Mọi normalized output phải tham chiếu raw input qua `provenance.raw_record_ids`.

Linker không sửa URL, title, body, content hash, publication hoặc provenance của article. Technology ID trong output phải thuộc taxonomy supplied. Complete output có đủ input article IDs; partial output phải giải thích lỗi.

Snapshot request không nhận label, risk probability hoặc thông báo sau cutoff. Nó nhận `RepoIdentity`, package ID, cutoff/horizon, releases, activities và coverage. Event/upload time, valid time và known available time không được sau cutoff; unknown availability bị từ chối ở feature boundary. Trạng thái yanked chỉ biết sau cutoff cũng bị từ chối.

Snapshot dùng khóa `(package_id, cutoff_at)` và `repo_group_id == repo_id`. Horizon là 12 tháng lịch. Các count theo cửa sổ 3/6/12 tháng phải không giảm khi cửa sổ dài hơn; releases total không nhỏ hơn release count 12m. Các metric dùng human contributor hay toàn bộ actor phải được chốt ở implementation feature; tên `contributor_count_12m` trong v0.1 là số actor người khác nhau có commit, bỏ actor xác định là bot, và trả null nếu không đủ actor mapping để tính chính xác.

### Boundary validation trong Python

```python
from shared.validation import validate_record, validate_collection_pair

request = validate_record(request_json)
result = validate_record(result_json)
validate_collection_pair(request, result)
```

Các checker còn lại: `validate_normalization_pair`, `validate_technology_link_pair`, `validate_snapshot_pair`. Chạy cả record validation và pair validation; kiểm tra record riêng lẻ không đủ chứng minh result thuộc đúng request.

### Sử dụng types trong TypeScript

```typescript
import type { Collector, CollectionRequest, CollectionResult } from "../shared/types";

// Collector implementation phải trả object đúng schema; type chỉ kiểm tra khi compile.
declare const collector: Collector;
declare const request: CollectionRequest;
const result: CollectionResult = await collector.collect(request);
```

Không dùng `as CollectionResult` thay cho validation dữ liệu untrusted. JS service có thể dùng JSON Schema Draft 2020-12 validator với kiểm tra format được bật; semantic rules như hash, chronology, ID matching và request/result matching phải port tương đương hoặc gọi Python validation trước khi lưu. JSON Schema/types chỉ biểu diễn structural constraints; các Pydantic validators là phần bắt buộc của contract runtime.

## 11 Ví dụ record hợp lệ

Tất cả ví dụ là dữ liệu **giả lập**, không phải kết quả crawl hoặc xác nhận trạng thái các dự án thực tế. Có 17 file đầy đủ, đã qua strict validation; không dùng để huấn luyện.

| Ví dụ | File |
| --- | --- |
| GitHub raw envelope | [github_raw_record.json](../examples/github_raw_record.json) |
| GitHub canonical repository | [github_repository.json](../examples/github_repository.json) |
| Technology entity | [technology_entity.json](../examples/technology_entity.json) |
| Article có published date nhưng không có giờ | [article.json](../examples/article.json) |
| Collector request/result | [collection_request.json](../examples/collection_request.json), [collection_result.json](../examples/collection_result.json) |
| Normalizer request/result | [normalization_request.json](../examples/normalization_request.json), [normalization_result.json](../examples/normalization_result.json) |
| Technology linker request/result | [technology_link_request.json](../examples/technology_link_request.json), [technology_link_result.json](../examples/technology_link_result.json) |
| Snapshot builder request/result | [snapshot_build_request.json](../examples/snapshot_build_request.json), [snapshot_build_result.json](../examples/snapshot_build_result.json) |

## 12 Quy trình thay đổi và kiểm tra trước khi ghép module

1. Sửa model/helper canonical; trao đổi thay đổi ảnh hưởng module khác trước khi merge.
2. Tăng version khi sửa cấu trúc hoặc semantics. Trong giai đoạn 0.x, đổi required field/ID/null/time rule phải tăng minor; sửa mô tả hoặc validator bug không đổi semantics có thể tăng patch.
3. Chạy export; commit model, JSON Schema, TypeScript, reference và fixture cùng một thay đổi. Không đổi generated file riêng.
4. Validate fixture và các cặp I/O; chạy contract tests. Module owner bổ sung fixture cho source case mới.
5. Consumer từ chối version chưa hỗ trợ. Không âm thầm rename key, coercion hoặc fill defaults.

```bash
python -m pip install -r requirements.txt
python scripts/export_contract.py
python scripts/export_contract.py --check
python scripts/validate_examples.py
python -m unittest discover -s tests -v
```

Python 3.11 trở lên. Pydantic được pin để generated schema không đổi theo version dependency. Có thể kiểm tra TypeScript bằng `tsc --noEmit --strict --target ES2020 shared/types.ts` khi TypeScript compiler có trong project. TypeScript không có runtime validator riêng trong bundle này; Python là reference validator đã kiểm tra.

Trước khi tích hợp, mọi thành viên cần thống nhất: cùng schema version; join bằng stable ID; không lẫn source/canonical naming; explicit null; UTC; không dùng current repository metadata làm historical features; coverage không bị đổi thành zero; raw provenance không bị bỏ.

## 13 Nguồn kỹ thuật

- Repository Scope v0.1 của RepoSeer, lập 05/10/2026: phạm vi nền và nguyên tắc historical evidence.
- [Pydantic JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/): sinh schema từ models.
- [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/): kiểm tra kiểu và hạn chế coercion.
- [JSON Schema Draft 2020-12 Validation](https://json-schema.org/draft/2020-12/json-schema-validation): structural validation và format semantics.
- [Python package name normalization](https://packaging.python.org/en/latest/specifications/name-normalization/): quy tắc ID package.

Các tên field, ID prefix, hashing rule và I/O trong tài liệu là quyết định thiết kế của RepoSeer; không phải field native của GitHub/PyPI.
