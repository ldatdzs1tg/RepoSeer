# PyPI analysis contract — 0.1.0

Contract bổ sung cho baseline, specialist agent, orchestrator và UI theo D11–D14
trong [pipeline](../architecture/pipeline.md). Các file, schema, public exports,
collection/normalization pipeline và data contract `0.1.0` hiện có được giữ nguyên.
Đây là định nghĩa và validation của đầu ra; không triển khai model, agent, inference,
status verifier hoặc dependency graph, và không tuyên bố chất lượng dự đoán.

## Nguồn chuẩn và cách dùng

- Python: [`src/reposeer/contracts/analysis.py`](../../src/reposeer/contracts/analysis.py).
- JSON Schema Draft 2020-12: [`analysis_contract.schema.json`](analysis_contract.schema.json).
- TypeScript: [`analysis_types.ts`](analysis_types.ts), dùng riêng với collection `types.ts`.
- Ví dụ tổng hợp: [`final_report.json`](../../examples/analysis/final_report.json).
- Các biến thể: [`examples/analysis/`](../../examples/analysis/).

Model Python là nguồn chuẩn cho hai artifact được sinh. Analysis version có namespace
riêng; không nâng version, mở rộng union hay thay đổi schema collection hiện có.
Không cần sửa `contracts/__init__.py`; import trực tiếp module mới:

```python
from pathlib import Path
from reposeer.contracts.analysis import ModelResult, AnalysisEvidence, validate_final_report

report = validate_final_report(Path("examples/analysis/final_report.json").read_bytes())
payload = report.model_dump_json(indent=2)
```

Baseline xuất `ModelResult` từ model đã đóng băng. Agent xuất `AnalysisEvidence` có
`origin="agent"`, citation và agent/model/prompt version. Orchestrator ghép các thành
phần thành `FinalReport` và gọi `validate_final_report` trước khi lưu/trả JSON.
UI nhận chính JSON đó, không tự tính hoặc ghép xác suất từ evidence.

```typescript
import type { FinalReport } from "./analysis_types";

function probabilityLabel(report: FinalReport): string {
  if (report.result_status === "predicted") {
    return `${(100 * report.prediction.probability).toFixed(1)}%`;
  }
  return "Không có dự đoán";
}
```

Tất cả khóa được khai báo đều required; khóa nullable vẫn phải có giá trị `null` khi
không áp dụng. Khóa lạ và coercion bị từ chối. Riêng khóa của một nhánh union chỉ tồn
tại trong nhánh đó. JSON Schema kiểm tra cấu trúc; Python validator còn kiểm tra thời
gian, tham chiếu, precedence, model/coverage và tính nhất quán giữa các trường.

## FinalReport và model result

Đơn vị phân tích là `package_id` chuẩn hóa `pypi:<name>`, không phải GitHub repository.
Mỗi report có `analysis_run_id`, `cutoff_at`, `generated_at`, `purpose`,
`horizon_months=12` và `horizon_end_at` sau đúng 12 tháng lịch, kể cả năm nhuận.
`mapping` giữ repo ID số ổn định, `repo_group_id=repo_id`, revision, package scope,
scope quality, thời hạn `[valid_from, valid_to)` và nguồn xác minh. Mapping chưa giải
quyết là `null`; mapping không có chứng minh lịch sử không đủ điều kiện dự đoán.

`FinalReport` là discriminated union theo `result_status`:

| `result_status` | Trường riêng | Ý nghĩa |
| --- | --- | --- |
| `predicted` | `mode_used`, `prediction`, `core_prediction` | Kết quả model hiệu lực và áp dụng được cho package/cutoff |
| `already_abandoned` | `mode_used=null`, `official_status` | Đã xác minh cessation có hiệu lực và được công bố trước hoặc tại cutoff |
| `scheduled_cessation` | `mode_used=null`, `official_status`, `within_horizon` | Kế hoạch cessation chính thức trong tương lai, chưa bị thu hồi tại cutoff |
| `mapping_unresolved` | `mode_used=null`, `reason_code`, `reason` | Chưa xác minh package↔repository mapping |
| `mapping_history_unverified` | như trên | Chưa chứng minh mapping hợp lệ ở cutoff |
| `unsupported` | như trên | Target hoặc trường hợp sử dụng không được hỗ trợ |
| `insufficient_evidence` | như trên | Thiếu bằng chứng, coverage hoặc applicability; gồm OOD |
| `failed` | như trên | Phân tích thất bại; giữ nguyên coverage, nguồn và lý do đã biết |

**`prediction`, `core_prediction` và mọi field xác suất không xuất hiện trên nhánh
known-status/no-score, kể cả với giá trị `null` hoặc `0`.** Contract này cụ thể hóa
quy tắc “no score” trong kiến trúc thành việc bỏ hoàn toàn field khỏi wire format.
UI không dùng số 0 để biểu diễn không có dự đoán và không đọc field trước khi narrow
`result_status`. Status unverified là tình trạng cần tiếp tục xét điều kiện dự đoán,
không tự động đồng nghĩa “active”, label 0 hoặc xác suất 0.

`ModelResult` có `result_status="predicted"`, xác suất hữu hạn trong `[0,1]`, risk
level và `ModelManifest`. Xác suất có nghĩa: confirmed maintenance abandonment trong
khoảng `(cutoff_at, horizon_end_at]`, theo population đã được đánh giá/calibrate.
Manifest giữ model ID/version/hash, E0/E1/E2 feature set, schema/preprocessing/
calibration/risk-threshold version, training/calibration population và thời điểm
training/freeze. Risk level do policy ngưỡng đã đóng băng tạo ra, không được suy từ
agent prose. Không có ngưỡng risk level chung do contract tự đặt.

`feature_source_ids` và `feature_evidence_ids` chỉ đến nguồn/evidence đã qua temporal
gate. `explanation` có thể `null`; nếu có phải là SHAP của đúng model version, ghi
output space, base value và attribution của từng feature. SHAP raw margin không phải
xác suất calibrated; không cộng attribution vào xác suất hoặc so SHAP giữa hai model.
`applicability` ghi policy, `in_domain/out_of_distribution/unknown`, reasons và limitations.
Chỉ `in_domain` được trả prediction. Validator không thay thế kiểm định/calibration.

## Mode, fallback và precedence

- CORE chọn model E0 trong `prediction`; `core_prediction=null`.
- ENHANCED chọn E1/E2 trong `prediction`, đồng thời giữ kết quả E0 độc lập ở
  `core_prediction`. Không trộn hoặc trung bình hai xác suất.
- Yêu cầu ENHANCED nhưng không đạt gate: `mode_used="CORE"`,
  `enhanced_status="fallback"` và `fallback_reason` không rỗng. Chỉ còn xác suất E0.
- CORE có `enhanced_status="not_requested"`. ENHANCED được sử dụng có `used`.
  Known-status/no-score dùng `not_run` nếu ENHANCED đã được yêu cầu; mọi kết quả model
  trước đó phải bị xóa khỏi report. Không giữ stale score để UI vô tình hiển thị.
- Official status được độc lập xác minh có precedence, kể cả khi phát hiện sau bước
  model hoặc khi chỉ yêu cầu external insights. Evidence chính thức đã xác minh không
  được nằm trong predicted/no-score report.

`OfficialStatus` giữ event type `ARCHIVED/MAINTENANCE_CEASED/REPLACED`,
`cessation_confirmed`, ngày announced/effective/verified/revoked, ledger revision,
verifier, policy và evidence IDs. `ARCHIVED` chỉ là cessation sau khi xác minh phạm vi
package và việc dừng bảo trì; cờ archived, im lặng, deprecation hay replacement đơn
thuần không đủ. Agent chỉ có thể đưa ra candidate/report-only evidence, không tự đặt
official status. Official evidence cần `origin="official"`, context direct-package,
`use="official_status"` và nguồn as-of hợp lệ.

Plan được scheduled khi effective date sau cutoff, dù ngoài horizon (khi đó
`within_horizon=false`). Plan đã đến hạn nhưng chưa xác minh cessation không tự động
thành already abandoned. Revocation có hiệu lực ở cutoff làm status cũ không còn hợp
lệ; revocation sau cutoff không viết lại status lịch sử. Việc phân giải nhiều revision,
xung đột và xác minh authority thuộc status verifier, trước khi tạo report.

## Cutoff và provenance

UTC phải đúng `YYYY-MM-DDTHH:mm:ss.sssZ`. `SourceProvenance` mở rộng `Provenance` hiện
có bằng `source_id`, native record ID, revision/content hash và query-set hash; vẫn giữ
URL, retrieved/valid/available time, temporal basis, raw record IDs và missing fields.
`source_id` nhận diện một revision, không chỉ một URL có thể thay đổi. Provider dùng
source taxonomy của data contract; forum/blog có thể là `web_article`, identity native
đặt trong `source_record_id`.

Prediction, mapping, official status và measured impact chỉ dùng nguồn có
`available_at <= cutoff_at`, `valid_at <= cutoff_at` cùng revision hoặc content hash.
Ngày published/event không thay thế availability proof. Observation có
`valid_at=retrieved_at`; dữ liệu hiện tại không được backdate. Event sau cutoff không
được dùng làm feature. Quote của plan có thể nêu effective date trong tương lai nhưng
chỉ dùng để xác định official status, không biến thành predictive feature.

Report có thể giữ late/unknown-availability evidence ở `use="report_only"`, kèm
`exclusion_reason`, với coverage `historical_validity="unverified"`. Những source này
không được xuất hiện trong feature lineage hoặc official-status evidence. Không source
nào được retrieved sau `generated_at`. `verified_at` có thể sau cutoff trong phân tích
lịch sử nếu revision của bằng chứng thực sự đã available ở cutoff.

Purpose không được đánh tráo:

- `current_inference`: model được triển khai tại thời điểm phân tích hiện tại.
- `retrospective_analysis`: model hiện tại áp dụng lên feature quá khứ; không phải
  đánh giá prospective accuracy.
- `prospective_backtest`: chỉ D10 xuất, bắt buộc `BacktestProvenance` với evaluation/
  fold ID, test window `(start_at, end_at]` và manifest hash; model gồm cả preprocessing/calibration phải
  được đóng băng trước test window. Các purpose còn lại có `backtest=null`.

## External/agent evidence và coverage

`AnalysisEvidence` ghi package, origin, direct/same-function/alternative context,
maintenance/functional-demand/migration/context signal, claim, event time, citations,
use, uncertainty và agent provenance khi cần. Citation có source ID, nguyên văn quote
và offsets `[start_offset, end_offset)` tính theo Unicode code point của source text.
Validator kiểm tra nguồn tồn tại và độ dài offset; producer/verifier phải đối chiếu
quote với nội dung revision thật. Schema không thể chứng minh authority, nội dung hay
truth của một claim chỉ bằng metadata được khai báo.

Không có risk score, prediction probability hoặc ground-truth label trong evidence.
E0 không dùng external hoặc specialist-agent features. E1 không dùng agent evidence.
E2 có thể dùng agent features đã kiểm tra. Report-only evidence không được tham chiếu
từ model. External insights có outcome riêng (`not_requested/complete/partial/
unavailable/failed`), hai trạng thái đầu không cần reason; các trạng thái còn lại bắt
buộc reason. Insights hoàn tất không có nghĩa discovery census đầy đủ và không làm
thay đổi xác suất model đã chọn.

Mỗi `AnalysisCoverage` gắn stage `core/external/agent/impact` và nguồn/window cụ thể:

| Chiều | Trường | Ý nghĩa |
| --- | --- | --- |
| Hoàn thành tác vụ | `status` | complete / partial / unavailable / not_requested |
| Độ đầy đủ nguồn | `source_coverage` | complete / partial / unknown, tương đối với scope đã khai báo |
| Hợp lệ lịch sử | `historical_validity` | verified / unverified / not_applicable |
| Quan sát | `records_observed` | null khi không biết; 0 chỉ khi census đầy đủ |
| Agent denominator | `eligible_documents`, `processed_documents` | Không suy ra tổng số từ top-K; processed ≤ eligible |
| Hạn chế | `missing_reason` | Bắt buộc khi status không complete |

Unavailable/not-requested không có count 0; counts phải null. Agent complete yêu cầu
mọi eligible document đã được xử lý. Prediction sources phải có coverage as-of rõ
ràng; partial core coverage chỉ được model dùng khi applicability/policy cho phép.
ENHANCED cần complete external census và E2 cần complete agent processing; nếu không
đạt thì producer phải chọn CORE fallback hoặc no-score. Validator không tự sửa dữ
liệu hoặc tự tạo score để che lỗi.

## Dependency impact tùy chọn

`dependency_impact=null` nghĩa không chạy/không yêu cầu. Đã yêu cầu nhưng thiếu corpus
hoặc thất bại thì trả `impact_status="unavailable"` và reason; branch này không có
count. `available/partial` có graph kind, graph as-of, corpus ID/size, source IDs và
unique direct/transitive dependents (transitive bao gồm direct, loại target và cycle
trùng lặp khi producer tính toán).

- `observed_potential`: các cạnh quan sát trong corpus, chưa resolve version/environment.
- `snapshot_resolved`: bắt buộc target version, environment và resolver version.
- Partial counts là lower bound và có missing reason. Complete chỉ đầy đủ trong corpus
  đã khai báo, không phải tổng toàn PyPI. Zero hợp lệ chỉ khi phép đo đủ scope.

Measured impact dùng graph không sau cutoff và source as-of chính `graph_as_of`;
không được dùng source hiện tại để mô tả graph quá khứ. Không có heuristic risk probability
trong impact. Impact failure không chặn prediction hợp lệ và impact không sửa xác suất.

## Ví dụ và kiểm tra

Tất cả ví dụ dùng package, URL `.test`, model, số liệu và evidence giả lập. Không có
API/LLM call, model thật hoặc performance claim.

| File | Trường hợp |
| --- | --- |
| `final_report.json` | Yêu cầu ENHANCED, fallback CORE; external/agent report-only insights; impact unavailable |
| `final_report_core.json` | Baseline CORE; không insights, không impact |
| `final_report_enhanced.json` | E2 selected + E0 retained; evidence/coverage và corpus impact |
| `final_report_already_abandoned.json` | Official cessation, hoàn toàn không prediction |
| `final_report_scheduled_cessation.json` | Official future plan, hoàn toàn không prediction |
| `final_report_no_score.json` | Mapping chưa giải quyết, reason rõ ràng, hoàn toàn không prediction |

Chạy từ root với môi trường dependency hiện có của repo:

```powershell
uv run --frozen python -m scripts.export_analysis_contract
uv run --frozen python -m scripts.export_analysis_contract --check
uv run --frozen python -m scripts.validate_analysis_examples
uv run --frozen pytest -q tests/contracts/test_analysis_contract.py
```

Exporter chỉ ghi hai artifact analysis mới, không ghi đè các artifact data contract.
`--check` chỉ đọc và trả exit 1 khi có drift. Tests mới tự được pytest thu thập; không
sửa workflow CI, dependency manifest, existing tests hay validator examples cũ.
Drift check chấp nhận LF/CRLF khi checkout trên Windows, không cần sửa `.gitattributes`.
