# C1–C7: thu thập external V2

## Mục đích

Dữ liệu cũ có thể không có bài external vì policy legacy chỉ dùng Hacker News,
một trang, một record và một candidate. Không có kết quả không chứng minh package
bị bỏ rơi. V2 tìm trên Hacker News, DEV.to, Stack Exchange, RSS và NewsAPI,
sau đó tải bài thật và đánh giá relevance từ nội dung.

Chạy enrichment để bổ sung external cho dữ liệu cũ. Lệnh chỉ đọc bằng chứng
PyPI/GitHub đã lưu; không gọi lại API PyPI/GitHub, kể cả bước preflight của enrichment.
Submission cũ giữ nguyên. Một submission `partial` vẫn dùng được nếu mapping đã verified.

D2 mới công bố `collection_policy.policy_id=distributed-v2` trong READY. Assignment
của run đó tự dùng V2. READY không có policy tiếp tục dùng `legacy-initial`;
không nâng cấp ngầm hoặc sửa receipt cũ.

## API key cá nhân

Mỗi người giữ key riêng trên máy; không gửi key cho trưởng nhóm, không commit `.env`,
không đưa `.env` lên Drive.

| Nguồn | Biến `.env` | Đăng ký và quyền tối thiểu |
|---|---|---|
| GitHub | `GITHUB_TOKEN` | [Tạo personal access token](https://github.com/settings/personal-access-tokens). Dùng tài khoản có quyền đọc repository công khai; không cần quyền ghi, admin hay repository private. Với classic token, public read không cần scope private `repo`. [Hướng dẫn chính thức](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens). |
| DEV.to | `DEVTO_API_KEY` | Đăng nhập [Settings → Extensions](https://dev.to/settings/extensions), tạo API key cá nhân. Dùng endpoint đọc; không dùng chức năng đăng/sửa bài. [Forem API](https://developers.forem.com/api/v1). |
| Stack Exchange | `STACKEXCHANGE_API_KEY` | [Đăng ký ứng dụng](https://stackapps.com/apps/oauth/register), lấy API key. Thu thập public search không cần quyền ghi hay OAuth của người dùng. [Authentication](https://api.stackexchange.com/docs/authentication) dùng `Authorization: Bearer ...`. |
| NewsAPI | `NEWS_API_KEY` | [Đăng ký key](https://newsapi.org/register). Chọn plan cho phép đọc `/v2/everything` và mục đích sử dụng của nhóm. [Giới hạn endpoint](https://newsapi.org/docs/endpoints/everything). |
| Hacker News | Không có key | API Algolia công khai. |
| RSS | `REPOSEER_RSS_FEEDS` | URL feed công khai HTTPS, không có API key. |

DEV.to và Stack Exchange có endpoint public; yêu cầu key cá nhân là quy định vận hành
của RepoSeer V2. `GNEWS_API_KEY` chưa được sử dụng vì chưa có GNews adapter.

## Setup trên Windows CMD

Mở CMD tại thư mục repo trên máy (nên đặt source code và workspace ngoài Drive):

```cmd
git switch feat/distributed-crawl-coordination
git pull --ff-only
uv sync --frozen
notepad .env
```

Điền các giá trị thực trong `.env` tại repo root:

```dotenv
GITHUB_TOKEN=
DEVTO_API_KEY=
STACKEXCHANGE_API_KEY=
NEWS_API_KEY=
REPOSEER_RSS_FEEDS=https://blog.python.org/feeds/posts/default
```

RSS nhận các URL ngăn cách bằng dấu phẩy, tối đa 32 feed, không trùng lặp. Feed mẫu
trên thuộc [Python Insider, blog chính thức Python](https://blog.python.org/).
Đây là ví dụ có nguồn gốc rõ ràng, không đại diện cho toàn bộ Python ecosystem.
Chọn thêm feed có liên quan nếu nhóm đã xác nhận nguồn; RSS chỉ tìm trong các entry
đang quan sát của những feed đã khai báo. Chỉ hỗ trợ RSS 2.0/Atom UTF-8, mỗi feed
không quá 2 MB. URL private/localhost/metadata hoặc feed có DTD/entities bị từ chối.

Không để giá trị mẫu như `changeme`, `your_key_here` hoặc chỉ có khoảng trắng.
Worker và subprocess đọc `.env` ở repo root, kể cả khi gọi worker từ thư mục khác.

## Kiểm tra API

```cmd
uv run --frozen python tools/coordination/worker.py check-providers
```

Lệnh không cần assignment, không collection và không ghi dữ liệu lên Drive.
JSON trả trạng thái từng provider và tên biến cần sửa; không trả giá trị key.

| Trạng thái | Cách xử lý |
|---|---|
| `ready` | Cấu hình và kiểm tra live đã đạt (`verification=live_verified`). |
| `missing_configuration` | Điền đúng biến trong `.env`, bỏ giá trị mẫu/rỗng. |
| `invalid_credentials` | Kiểm tra hoặc cấp lại key. Stack Exchange phải chứng minh quota của ứng dụng; HTTP 200 với quota anonymous chưa đủ. |
| `provider_unreachable` | Kiểm tra mạng/DNS/provider; thử lại sau. |
| `quota_exhausted` | Chờ quota được cấp lại hoặc đổi plan; không chia sẻ key. |
| `provider_plan_restricted` | Kiểm tra quyền endpoint, thời gian tìm kiếm và giới hạn plan NewsAPI. |
| `invalid_feed` | Sửa URL, bỏ URL trùng/private, kiểm tra XML và kích thước feed. |
| `provider_not_implemented` | Kiểm tra đang dùng đúng branch/source; không tiếp tục bằng cách bỏ nguồn. |

Preflight tách hai loại kiểm tra. Thiếu biến/rỗng/placeholder/URL feed sai là
`configuration_incomplete`: chưa bắt đầu công việc V2 mới. Khi cấu hình tĩnh đủ,
quota, rate limit hoặc lỗi mạng của một nguồn chỉ trì hoãn nguồn đó. Credentials
external không hợp lệ tạo `configuration_blocked` cho nguồn tương ứng, các nguồn
khỏe vẫn chạy. GitHub không sẵn sàng sẽ chặn collection PyPI/GitHub mới; enrichment
trên source đã verified không gọi GitHub và không phụ thuộc quota GitHub.

Kết quả vẫn có `ready`, `providers`; bổ sung `configuration_valid`, `github_ready`,
`available_external_sources`, `deferred_external_sources` và `provider_status`.
`retry_after=null` nghĩa là chưa biết thời điểm reset, không phải dự đoán ngày mai.
`check-providers` có thể exit nonzero khi chưa đủ năm nguồn; worker vẫn dùng trạng
thái từng provider để tiếp tục nguồn khỏe. Preflight chỉ probe các nguồn còn cần
network trong invocation, không probe lại provider đã có completion verified.

## Enrich dữ liệu đã crawl

Chỉ định thư mục worker của mình, không nhập từng run/repo:

```cmd
set "SUBMISSIONS_DIR=G:\My Drive\Intro2DS\RepoSeer-Data\submissions\C1"
uv run --frozen python tools/coordination/worker.py enrich-drive --submissions-dir "%SUBMISSIONS_DIR%" --dry-run
uv run --frozen python tools/coordination/worker.py enrich-drive --submissions-dir "%SUBMISSIONS_DIR%"
```

C2–C7 thay `C1` bằng mã tương ứng. Lệnh quét tất cả sampling runs và repository parts,
verify checksum/READY, đối chiếu assignment D2 khi còn truy cập được, rồi xác minh
mapping từ raw PyPI/GitHub. Không dùng tên thư mục để tự suy ra identity.

`--dry-run` không gửi request, không tạo workspace/output. Nó verify nguồn/receipts
và in số package/provider dự kiến; không phải bằng chứng API thực sự hoạt động.

External lần đầu giữ nguyên trong `part_001/raw/external/`. Enrichment nằm cạnh
source part, trong cùng repository subtree, không nằm trong checksum nguồn:

```text
submissions/C1/<sampling_run_id>/<repository_id>/
  part_001/
    raw/external/{discovery,documents}.jsonl
    COLLECTION_INPUT.json
    CHECKSUMS.sha256
    READY.json
  supplements/part_001/external-v2/
    packages/<sha256-canonical-package-id>/
      hackernews/
      devto/
      stackexchange/
      rss/
      news/
    state/<sha256-canonical-package-id>/
      hackernews.json
      devto.json
      stackexchange.json
      rss.json
      news.json
```

Mỗi thư mục provider có `ENRICHMENT_INPUT.json`, `raw/external/`, minimal identity
`raw/pypi/packages.jsonl`, `raw/github/repositories.jsonl`, `processed/`, `metadata/`,
`reports/external_enrichment.json`, `CHECKSUMS.sha256` và READY riêng. Package directory
là SHA-256 của canonical ID, ví dụ `pypi:flask`, không phải tên package tùy ý.
Các file identity giữ raw ID/timestamp gốc; không copy GitHub activity datasets.

Workspace mặc định: `C:\RepoSeerData\supplements`. Có thể đổi bằng
`--data-root "C:\RepoSeerData\external-work"`; workspace phải nằm ngoài Drive.
Dùng lại workspace này khi retry; checkpoint cũ được giữ nguyên, không tự xóa.
Source READY/checksum và mapping evidence được gắn trong receipt của từng provider.
Chỉ publish READY sau khi kiểm tra toàn bộ output. Lần chạy lại verify rồi skip
receipt hợp lệ, kể cả khi online state bị stale. State ghi complete mà không có
receipt hợp lệ không chứng minh hoàn thành.

| Provider status | Lần chạy tiếp theo |
|---|---|
| `pending`, `running` | Kiểm tra checkpoint và availability trước khi tiếp tục. |
| `complete`, `complete_no_results` | Verify receipt rồi skip, không gọi lại provider. |
| `budget_completed` | Terminal trong V2; không tăng ngân sách hoặc retry query vì còn pagination. |
| `quota_deferred` | Chỉ nguồn đó chờ quota; invocation sau kiểm tra lại. |
| `retryable_failure` | Retry có giới hạn, giữ cursor và observations đã lưu. |
| `configuration_blocked` | Sửa key/quyền truy cập trước lần chạy tiếp. |
| `requires_inspection` | Nhờ operator kiểm tra; không xóa checkpoint hoặc tự sửa hash. |

Ví dụ NewsAPI hết quota: HN/DEV.to vẫn tải và xử lý bài, Stack Exchange/RSS vẫn
được ghi nhận kết quả hợp lệ; bốn receipt có thể publish ngay, NewsAPI giữ state
`quota_deferred`. Những package còn lại trong cùng invocation không tiếp tục gọi
NewsAPI. Khi chạy lại, chỉ phần chưa hoàn thành được xử lý.

Budget V2 là tối đa 30 raw observations/provider/package, tối đa ba trang/query,
và tổng 20 candidate URL/package tính cả initial cùng supplements. `max_pages`
không có nghĩa pagination upstream đã complete. Candidate priority và round-robin
provider được giữ; URL/native identity đã có valid observation được reuse, không
fake record hay thay timestamp. Những provider khác phát hiện cùng bài giữ liên kết
query qua `document_associations` và discovery raw records trong receipt.

Summary có `packages_planned`, `packages_processed`, `pending_packages`,
`providers_completed`, `providers_budget_completed`, `providers_deferred`,
`providers_failed`, document/relevance counts và `failures`. Counts documents của
package loại trùng raw ID giữa initial/supplements. `partial` khác source crawl
failed. Quota đơn thuần không rollback provider khỏe và không làm CLI exit lỗi;
configuration/integrity hoặc trạng thái cần operator xử lý trả exit nonzero.

Nếu publish Drive bị ngắt nhưng local READY còn hợp lệ, lần sau verify và copy tiếp
mà không gọi API lại. Discovery resume từ durable jobs cùng execution scope. Content
fetch đã lưu được reuse; URL lỗi mạng có thể retry mà không chiếm slot mới. Nếu ghi
content/processing bị ngắt và không chứng minh resume an toàn được, báo
`requires_inspection`. Không tự ghi đè raw/READY/checksum đã seal.

## Coverage và đọc downstream

D2/operator là single writer của bảng tổng hợp, C1–C7 không sửa master mapping:

```cmd
uv run --frozen python tools/coordination/dispatcher.py external-report --drive-root "%DRIVE_ROOT%"
```

Lệnh tạo `screening/external_coverage.csv` từ source/receipt verified và state, phân
biệt sampling run/worker/package/source part. Đây là derived view, không phải source
of truth. Không thêm cột vào `screening/mapping.csv`, không đổi mapping metadata.

Trong repo, dùng `tools.coordination.external.resolve_verified_external_inputs`
để nhận document paths, observations đã loại trùng raw ID, canonical/native indexes,
provenance, targets và coverage. Helper chỉ nhận receipt có READY/checksum hợp lệ;
không đọc raw chưa seal làm evidence. Có thể lọc package hoặc cutoff theo thời điểm
observation, không dùng publication time để giả định historical availability.
`StoragePaths.processing_inputs()` của source đã published cũng dùng resolver này.

Xử lý source đã seal phải ghi ra output local riêng, không sửa `processed/` nguồn:

```cmd
uv run --frozen reposeer process --data-dir "%SOURCE_PART%" --output-dir "C:\RepoSeerData\analysis"
```

## Chạy assignment mới

Lệnh cũ giữ nguyên:

```cmd
set "ASSIGNMENT=G:\My Drive\Intro2DS\RepoSeer-Data\assignments\C1\<sampling_run_id>.csv"
uv run --frozen python tools/coordination/worker.py crawl-drive --assignment "%ASSIGNMENT%"
```

Không thêm source flags, limits hoặc policy. Worker tự đọc policy từ D2 READY đã verify.
V2 dùng tối đa 4 trang/300 records cho collection, discovery tối đa 3 trang/query và
150 raw observations/target, rồi crawl tối đa 20 candidate URLs. Khi dùng nhiều nguồn,
discovery chia ngân sách records theo provider; candidate selection ưu tiên identity
metadata rồi round-robin provider trong cùng mức ưu tiên. Đây là ngân sách tối đa,
không phải mục tiêu phải tìm đủ bài.

Run legacy vẫn dùng Hacker News với giới hạn gốc; submission READY được verify/skip,
không crawl lại PyPI/GitHub. Sau khi hoàn thành hoặc verify các source parts của
assignment, `crawl-drive` dùng cùng planner với `enrich-drive` và tự enrich tối đa
**một package còn thiếu trong batch hiện tại mỗi invocation**. Ưu tiên package chưa
có supplement và thiếu nhiều nguồn, rồi canonical package ID để tie-break. Lần sau
chọn package còn thiếu tiếp theo hoặc provider deferred đã phục hồi. Không quét
hàng trăm package lịch sử ngoài assignment; dùng `enrich-drive` để xử lý toàn bộ.
Assignment V2 tự preflight: thiếu cấu hình tĩnh phải dừng, quota một nguồn không
chặn các nguồn khỏe. Explicit sources của CLI cũ vẫn dùng được. Người gọi trực tiếp
`reposeer collect-dataset` chỉ bật gate V2 khi chọn `--policy distributed-v2`.

## Hạn chế và bảo quản dữ liệu

RSS không khôi phục lịch sử web. NewsAPI chỉ cung cấp phần dữ liệu mà plan cho phép;
adapter không tuyên bố historical coverage đầy đủ. Theo [trang pricing chính thức](https://newsapi.org/pricing), tại ngày 2026-10-30 plan Developer có 100 request/ngày, dữ liệu trễ 24 giờ và tìm trong một tháng gần nhất; kiểm tra lại giới hạn và điều kiện sử dụng của plan trước khi chạy. Bài tải hôm nay chỉ được xác minh
là quan sát hôm nay (`observation_only`), không mặc định đã có cùng nội dung ở ngày đăng.
Candidate được search ra, verified repository scope và relevance là ba việc riêng biệt.
Chỉ nội dung substantive đủ bằng chứng mới được đánh giá `relevant`; không tạo nhãn
abandoned hay chạy agent/labeling từ kết quả tìm kiếm.

- Không xóa `READY.json`, sửa `COLLECTION_INPUT.json` hoặc thay `CHECKSUMS.sha256`.
- Không chạy nhiều crawler trên cùng assignment; không sửa output của worker khác.
- Không xóa local checkpoint khi interrupted, không copy `.env` lên Drive.
- Bảo đảm Drive đã tải đủ input. Worker cần quyền đọc screening và các assignment CSV
  để đối chiếu, quyền ghi submission của mình và namespace enrichment của mình.
- Policy mới trong tương lai phải có output namespace mới; không sửa `external-v2` đã published.