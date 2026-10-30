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

Thiếu một nguồn bắt buộc thì collection V2 dừng trước khi tạo output mới, exit nonzero.
Preflight chạy một lần mỗi invocation có công việc network mới. Khi tất cả output
đã có READY hợp lệ, worker chỉ verify/skip và không kiểm tra API lại.

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

`--dry-run` không gửi request, không tạo workspace/output. Nó chỉ kiểm tra nguồn,
cấu hình tĩnh và in số part dự kiến; không phải bằng chứng API thực sự hoạt động.

Output nằm riêng tại:

```text
RepoSeer-Data/external-enrichments/external-v2/C1/<sampling_run_id>/<repository_sha256>/part_001/
  ENRICHMENT_INPUT.json
  raw/external/discovery.jsonl
  raw/external/documents.jsonl
  raw/pypi/packages.jsonl
  raw/github/repositories.jsonl
  processed/
  metadata/
  reports/external_enrichment.json
  CHECKSUMS.sha256
  READY.json
```

Hai file identity raw chỉ chứa bằng chứng tối thiểu đã copy, giữ nguyên raw ID và
thời điểm gốc; không copy activity datasets. `ENRICHMENT_INPUT.json` liên kết tới
source bằng đường dẫn, integration ID và hash của READY/checksum nguồn.

Workspace local mặc định là `C:\RepoSeerData\external-enrichments`. Có thể dùng
`--data-root "C:\RepoSeerData\external-work"` để đổi workspace; phải nằm ngoài Drive.
Dùng lại đúng workspace khi retry. File được copy/verify trước, READY được công bố cuối.
Chạy lại sẽ verify/skip output hợp lệ, không tạo bản trùng hoặc gọi external API lại.

Đọc JSON summary: `completed`, `no_candidates`, `skipped_existing`, `partial`, `failed`,
candidate/document counts, ba relevance decisions và thống kê `providers`.
`no_candidates` chỉ là không có kết quả sau khi providers đã được gọi thành công.
Quota/network/parse failure trả partial/failed và exit nonzero; không công bố READY
thành công. Khi hết quota hoặc bị hạn chế quyền/plan, worker dừng các part network còn lại và báo `deferred`; không tiếp tục tiêu tốn request cho cùng lỗi. Báo cáo chi tiết vẫn ở workspace local.

Nếu interrupted: giữ nguyên checkpoint và chạy lại cùng lệnh. Discovery có thể
resume từ durable jobs. Nếu content capture/processing bị ngắt giữa chừng và không
chứng minh resume an toàn được, worker báo `requires_inspection`; nhờ D2 kiểm tra,
không tự xóa output/checkpoint. Nếu local READY hoàn chỉnh nhưng Drive copy chưa xong,
chạy lại sẽ verify và publish tiếp. `source_fingerprint_mismatch` hoặc checksum conflict
cần được kiểm tra; không tự sửa hash hay overwrite output đã published.

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

Run legacy vẫn dùng Hacker News với giới hạn cũ và in thông báo cần enrichment.
Submission đã hoàn tất tiếp tục được verify/skip. Assignment mới có V2 sẽ tự preflight;
thiếu API thì phải dừng. Explicit sources của CLI cũ vẫn dùng được. Người gọi trực tiếp
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