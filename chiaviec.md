# Phân chia công việc — Team 3 thành viên

## 1. Mục tiêu chung

Xây dựng hệ thống multi-agent điều tra khiếu nại thương mại điện tử L3B:
resolve đúng đơn hàng, lấy evidence qua MCP, phân tích nghiệp vụ, phối hợp
qua A2A và xuất JSON/trace đúng public contracts.

Tài liệu tham chiếu:

- [README.md](README.md): yêu cầu đề tài, lệnh chạy và cách nộp bài.
- [ARCHITECTURE.md](ARCHITECTURE.md): thiết kế agent, A2A, tool permissions và retry.
- [Public schemas](contracts/schemas/): cấu trúc chuẩn, không tự ý sửa.

**Trạng thái khi lập kế hoạch:** pha 2 đã có thiết kế và test khóa schema.
Các module agent dưới đây là phần cần triển khai, chưa phải chức năng đã chạy.
Điền tên/GitHub của từng người vào bảng trước khi bắt đầu.

## 2. Phân công chính

| Thành viên | Tên / GitHub | Phạm vi chính | Nhánh đề xuất |
| --- | --- | --- | --- |
| TV1 — phụ trách tích hợp | Điền tên | Coordinator, Entity/Customer, A2A, evidence collector, trace, tích hợp | feature/coordinator-entity |
| TV2 — đơn hàng và giao vận | Điền tên | Order/Item Agent, Shipment Agent | feature/order-shipment |
| TV3 — thanh toán và quyết định | Điền tên | Payment Agent, Policy/Conflict Agent, kiểm chứng nghiệp vụ | feature/payment-policy |

TV1 hỗ trợ TV3 phần kiểm tra schema và evidence của Verifier để cân bằng tải.
Mỗi người chịu trách nhiệm code, test phù hợp và tài liệu cho phần mình làm.

### TV1 — Coordinator, Entity và hạ tầng chung

- Chốt kiểu message A2A, chữ ký hàm specialist và kết quả nội bộ cùng cả team.
- Triển khai Entity/Customer Agent: kiểm chứng ID, resolve/reject candidate,
  customer context; giữ ambiguous/not_found khi chưa đủ evidence.
- Triển khai evidence collector: discovery metadata/inputSchema, phân quyền
  tool, validate response, registry theo case, cache, retry, timeout và budget.
- Điều phối specialist, thu findings, chuyển policy và verifier trong solve_case().
- Quản lý trace; không phát trùng case_received/case_finalized do CLI đã phát.
- Triển khai phần schema/evidence của Verifier: đúng case, ref trong registry,
  liên kết trace và output, đúng public schema.
- Tích hợp PR, chạy toàn bộ input, validate và đóng gói bài nộp.

**Bàn giao:** một case chạy xuyên suốt với agent thật; evidence không bị trộn
case; trace phản ánh các lần giao việc và dùng evidence thực tế.

### TV2 — Order/Item và Shipment

- Phân tích order, item, seller và product context trong entity scope đã resolve.
- Xây dựng timeline giao hàng; phân biệt seller delay, logistics delay,
  lost/returned/conflicting/insufficient_evidence theo dữ liệu thực tế.
- Cung cấp affected entities thuộc phạm vi đơn hàng và giao vận.
- Trả finding có cấu trúc, evidence_refs và mâu thuẫn quan sát được cho coordinator.
- Không tự suy diễn nguồn chịu trách nhiệm khi thiếu bằng chứng; đưa đề xuất
  cho policy tổng hợp quyết định cuối.
- Kiểm thử giao đúng hạn, seller/logistics delay và timeline thiếu/mâu thuẫn.

**Tool được phép:** get_order, get_order_items, get_product_context, get_sellers
cho Order Agent; get_shipment_summary, get_order_items cho Shipment Agent.
Mọi lần lấy evidence đi qua collector chung.

**Bàn giao:** hai agent gọi độc lập được qua giao diện đã thống nhất, findings
có evidence và không ảnh hưởng trực tiếp tới file output cuối.

### TV3 — Payment, Policy/Conflict và kiểm chứng nghiệp vụ

- Đối soát capture, split payment, duplicate capture và refund timeline.
- Tính captured/refunded/refundable totals bằng Decimal nội bộ; không coi
  dữ liệu thiếu là số tiền đã xác nhận bằng 0.
- Áp dụng policy MCP để giải quyết source conflict, xác định issue,
  responsibility, financial resolution và resolution actions.
- Giữ conflict chưa giải quyết khi policy/evidence không đủ để chọn nguồn.
- Triển khai hàm kiểm tra nghiệp vụ cho Verifier: tổng tiền, refund không trùng,
  action/status/responsibility nhất quán, confidence hợp lý.
- Kiểm thử split payment hợp lệ, duplicate capture, refund pending/failed,
  thiếu số tiền và conflict chưa giải quyết.

**Tool được phép:** get_order_payments, get_payment_timeline, get_refund_timeline
cho Payment Agent; get_policy cho Policy Agent. Verifier không gọi MCP.

**Bàn giao:** payment findings, policy result và các kiểm tra nghiệp vụ; kết quả
thiếu evidence phải được biểu diễn trung thực theo schema.

## 3. File sở hữu và tránh xung đột

Cấu trúc đề xuất cho phần triển khai:

```text
src/student_agent/
├── workflow.py              # TV1: coordinator và tích hợp
├── a2a.py                   # TV1: message và kết quả nội bộ
├── evidence_collector.py    # TV1: permission/cache/retry/budget
└── agents/
    ├── entity.py            # TV1
    ├── order.py             # TV2
    ├── shipment.py          # TV2
    ├── payment.py           # TV3
    ├── policy.py            # TV3
    ├── business_checks.py   # TV3: kiểm chứng nghiệp vụ
    └── verifier.py          # TV1: schema/evidence + gọi business_checks
```

- TV1 quản lý thay đổi workflow.py, a2a.py, mcp_gateway.py, trace.py và CLI.
- TV2/TV3 đề xuất thay đổi giao diện chung qua PR hoặc trao đổi trước khi sửa.
- Test tách theo module: test_entity, test_order, test_shipment, test_payment,
  test_policy, test_verifier; người sở hữu chức năng bổ sung test tương ứng.
- Không sửa public schemas hoặc schema-lock.json để làm output sai pass test.
- Thay đổi kiến trúc được cập nhật trong ARCHITECTURE.md và review cùng PR.

## 4. Thống nhất giao diện trước khi code song song

Cả team cần chốt các mục sau trong PR nền tảng của TV1:

- Chữ ký hàm specialist nhận case, entity scope, collector và trace/context.
- Message envelope theo ARCHITECTURE.md: message_id, case_id, sender, recipient,
  task_type, status, entity_scope, evidence_refs, payload, error_code.
- Payload từng specialist có kiểu dữ liệu rõ ràng; không trả văn bản tự do
  rồi buộc coordinator đoán field hoặc parse lại.
- Trạng thái thành công/thất bại và cách biểu diễn missing/conflicting data.
- Kiểu tiền nội bộ, cách chuyển sang JSON number và quy tắc làm tròn theo policy.
- Agent nào phát task_assigned, handoff, tool_result_consumed và verification_completed.

Message A2A là dữ liệu nội bộ, không thêm nguyên envelope vào JSON nộp bài.
TV2/TV3 có thể dùng collector giả cho unit test trong lúc TV1 làm hạ tầng;
mock evidence chỉ dùng trong test, tuyệt đối không đưa vào submission.

### Quyền tạo dữ liệu output

| Field / nhóm field | Nguồn chịu trách nhiệm chính |
| --- | --- |
| schema_version, case_id | TV1 / Coordinator |
| entity_resolution, customer_context | TV1 / Entity Agent |
| affected_entities | TV1 tổng hợp scope; TV2/TV3 cung cấp IDs theo domain |
| shipment_analysis | TV2 / Shipment Agent |
| payment_analysis | TV3 / Payment Agent |
| assessment, root_cause_analysis, financial_resolution, resolution_actions | TV3 / Policy Agent dựa trên findings |
| data_conflicts | Các specialist phát hiện, TV3 / Policy Agent tổng hợp |
| evidence_refs | TV1 tổng hợp refs thực sự được sử dụng và kiểm tra scope |
| claim_assessments nếu xuất | TV3 tổng hợp từ findings, tuân theo schema |

Coordinator lắp output; Verifier kiểm tra trước khi trả về CLI. Specialist
không tự ghi outputs/<case_id>.json hoặc tự sửa kết quả của agent khác.

## 5. Các mốc triển khai

| Mốc | TV1 | TV2 | TV3 | Điều kiện hoàn tất |
| --- | --- | --- | --- | --- |
| M0 — giao diện chung | A2A types, collector interface, chọn case tích hợp | Kiểm tra input/tool metadata cần dùng | Kiểm tra input/tool metadata cần dùng | Team thống nhất contract nội bộ và file sở hữu |
| M1 — một case xuyên suốt | Entity, collector, coordinator, trace | Order + shipment cơ bản | Payment + policy cơ bản | MCP thật → agents → verifier → JSON/trace hợp lệ |
| M2 — case khó | Ambiguous entity, timeout/cache/budget | Delay, missing/conflicting timeline | Split/duplicate/refund/conflict | Test các nhánh nghiệp vụ quan trọng và review chéo |
| M3 — bài nộp | Chạy đủ case, validate/package | Sửa lỗi order/shipment | Sửa lỗi payment/policy | ZIP hợp lệ và chọn submission final trên workspace |

Điền deadline các mốc sau khi biết lịch nộp của lớp. Không đợi hoàn thiện toàn
bộ module mới tích hợp: đưa PR nhỏ lên ngay khi đạt một hành vi kiểm chứng được.

## 6. Quy trình GitHub và review

1. Mỗi người tạo nhánh từ nhánh chung mới nhất; không đổi tên repo gốc.
2. TV1 đưa giao diện nền tảng lên trước để TV2/TV3 cùng dùng.
3. Mỗi PR ghi: hành vi thay đổi, case/test kiểm chứng, hạn chế còn lại.
4. Cần ít nhất một người khác review trước khi merge; TV1 điều phối tích hợp.
5. Đồng bộ nhánh sau thay đổi giao diện chung; không tự ý force-push nhánh chung.

| Người làm | Người review chính | Trọng tâm review |
| --- | --- | --- |
| TV1 | TV2 | Resolve entity, scope, handoff và trace |
| TV2 | TV3 | Evidence hỗ trợ kết luận giao vận và trách nhiệm |
| TV3 | TV1, TV2 hỗ trợ phép tính | Payment/refund, policy, conflict và consistency |

Không commit .env, API key hoặc debug log chứa thông tin xác thực. Không gửi
key qua PR/comment. Mỗi người cấu hình môi trường riêng theo hướng dẫn lớp.

## 7. Tiêu chí bàn giao mỗi phần

- Dùng đúng giao diện và tool permissions đã chốt.
- Không đoán tool arguments; lấy từ discovery inputSchema và dữ liệu thật.
- Evidence lấy qua MCP, giữ nguyên ref và không dùng chéo case/run.
- Có xử lý thiếu dữ liệu và lỗi liên quan; không trả đáp án bịa để pass schema.
- Có test có ý nghĩa cho hành vi nghiệp vụ hoặc invariant quan trọng.
- Ruff pass; chạy test liên quan và ghi rõ kết quả trong PR.
- Không ghi prompt/chain-of-thought/raw secrets vào trace.
- Không sửa ngoài phạm vi sở hữu khi chưa thống nhất.

## 8. Kiểm tra tích hợp và nộp bài

Chạy từ root repo với môi trường đã kích hoạt:

```text
python -m ruff check .
python -m pytest -q
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Lưu ý: test_repository_contains_no_competition_payload kiểm tra repo phát hành
không có input; test này sẽ fail khi đã tải case-set.json vào workspace. Ghi
rõ nguyên nhân khi báo kết quả, không xóa input hoặc đổi test chỉ để báo pass.
Lỗi quyền thư mục tạm của pytest cần xử lý theo môi trường từng máy.

ZIP chỉ chứa manifest.json, trace.jsonl và outputs/<case_id>.json. TV1 chịu
trách nhiệm upload tại /l3b và chọn submission final sau khi team review.

Ưu tiên sửa đúng entity và evidence trước, sau đó nghiệp vụ/consistency, rồi
tối ưu MCP calls. Pass schema không đồng nghĩa đúng nghiệp vụ hoặc đạt điểm cao.
