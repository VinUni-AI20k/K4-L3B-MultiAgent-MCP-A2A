# L3B Architecture Record

## 1. System overview

Input → Entity/customer → Order/product → Shipment → Payment/refund → Policy
→ Conflict resolver/coordinator → Independent verifier → JSON output + observable trace.

Trước MCP, CLI gọi POST /api/v2/runs với variant_id=l3b để mở phiên audit theo đúng API của workspace.

Các vai trò chạy tuần tự bằng Qwen3-4B, trao đổi các findings có evidence_refs qua coordinator.
Đây là A2A nội bộ trong một process, không triển khai server A2A qua mạng. Không dùng framework
bên ngoài để ẩn luồng gọi; tất cả MCP request đi qua EvidenceGateway và được server audit.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Case, candidates, customer hint | Đối chiếu candidate với dữ liệu đơn và khách | order/customer/candidate/resolve/search | Findings + refs |
| Coordinator | Case và reports | Phân việc, quản lý call budget, tổng hợp | Không trực tiếp gọi MCP | Phân việc theo case_id |
| Order/product | Case và evidence | Item, seller, product context | order/item/product/seller | Findings + refs |
| Shipment | Case và evidence | Deadline, bàn giao, giao hàng | shipment/shipping/delivery/tracking | Findings + refs |
| Payment/refund | Case và evidence | Capture, split payment, refund | payment/refund/capture/transaction | Findings + refs |
| Policy | Case, policy version, evidence | Đọc policy và source precedence | policy | Findings + refs |
| Conflict resolver | Evidence và reports | Tổng hợp draft và biểu diễn conflict | Không gọi MCP | Draft output |
| Verifier | Case, raw evidence, draft, schema | Kiểm tra độc lập và yêu cầu sửa | Không gọi MCP | Approve hoặc corrected output |

Chỉ tool được discovery, có tiền tố đọc và nằm trong whitelist tên tool của role mới được phép gọi. Annotation
readOnly=false hoặc destructive=true bị chặn. Arguments được validate theo inputSchema.
Danh sách tool chưa biết trước; tool không khớp whitelist bị từ chối, không đoán API.

## 3. Entity resolution và A2A protocol

Model đối chiếu mọi candidate với evidence thay vì tin claimed_order_id hoặc pattern của ID.
Handoff gồm findings (fact, evidence_refs), gắn với case đang xử lý. Report không phải evidence
mới. Không lưu nội dung suy luận riêng. Unknown/ambiguous phải dùng needs_investigation.
Không dùng threshold confidence cố định chưa hiệu chuẩn; kết luận và confidence phụ thuộc
bằng chứng, sau đó verifier kiểm tra lại. Candidate rejected phải nằm trong input và không
trùng tập resolved. Việc xác nhận quan hệ customer/order về ngữ nghĩa vẫn do model verifier.

## 4. Evidence lifecycle

Discovery cache theo connection; evidence cache chỉ trong một Investigation/case, khóa là
(tool name, normalized arguments). case_id được gắn bắt buộc; yêu cầu gọi chéo case bị chặn.
MCP response phải pass schema. Giữ nguyên evidence_ref, result_hash, domain, data từ server.
Mỗi lần role sử dụng kết quả gọi/cache có tool_result_consumed. Reference trong claim phải
nằm trong output evidence_refs và trong ledger đã nhận ở case hiện tại. Không kiểm chứng được
team/run ownership chỉ từ response schema; server audit là nguồn xác thực provenance cuối.

Conflict resolver dùng policy precedence khi có đủ dữ liệu; conflict chưa giải quyết có
selected_source=null, không tự coi nguồn bất kỳ là chân lý. Dữ liệu case, tool description và
nội dung evidence được đặt trong payload dữ liệu, kèm system instruction chống prompt injection.
Đây là phòng vệ nhiều lớp, không phải chứng minh loại bỏ mọi prompt injection.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace |
| --- | ---: | --- | --- |
| MCP timeout/invalid result | Tối đa 2 attempts cùng request | Báo thiếu evidence | Handoff INCOMPLETE nếu hết vòng |
| Entity ambiguous/not found | Trong 5 vòng/role | needs_investigation | Handoff + draft |
| Source conflict | Tối đa 3 lượt verifier | Giữ conflict hoặc dừng nếu không hợp lệ | Không có verification PASS giả |
| Invalid model JSON | 1 retry mỗi generation | Dừng nếu vẫn lỗi | Không ghi output lỗi |
| Model/network unavailable | Không tự retry HTTP | Dừng với thông báo cấu hình | Không tạo evidence |

Tối đa 24 MCP attempts/case (kể cả thất bại), 4 requests/vòng, 5 vòng/role, timeout MCP
45 giây. Role hết vòng chuyển report incomplete. Cache hit không tính thêm MCP attempt.
Các model requests chạy tuần tự; model timeout mặc định 180 giây. Verifier tối đa 3 lượt.

## 6. Verification invariants

Python kiểm tra JSON schema; case ID; tập evidence refs và claim IDs; resolved/rejected
không giao nhau; affected orders đúng resolved scope; identifiers có trong evidence; customer
và related orders; late seller IDs; số tiền refundable không vượt capture trừ refunded;
tổng refund lines bằng recommended refund dùng Decimal; refund dương có policy evidence
và không vượt funds đã biết; selected_source thuộc sources. Không ghi output khi fail.

Verifier model riêng nhận raw evidence, không nhận nội dung hội thoại suy luận của specialist.
Nó kiểm tra timeline, source precedence, trách nhiệm, eligibility, duplicate/pending refunds,
customer ownership và confidence. Cùng model không đảm bảo độc lập thống kê; test mock không
đo accuracy thực tế. Cần đánh giá với evidence thật trước khi khẳng định chất lượng thi.

## 7. Reproducibility

Qwen3-4B qua Ollama native /api/chat, think=false, temperature=0, context mặc định 16384,
num_predict tối đa 6000. Tool planner và output cuối dùng JSON schema để ràng buộc định dạng. Không đảm bảo bit-for-bit giữa runtime/hardware khác nhau.
Model và tham số nằm trong .env (không commit); parameter-count khai báo phải trong (0,10].
Python >=3.11; dependency constraints trong pyproject.toml, snapshot phiên bản đã kiểm thử
trong requirements-lock.txt nếu có. Concurrency=1; không đặt random seed riêng.
Lệnh cài/chạy, các cấu hình và giới hạn xem HUONG_DAN_CHAY.md.
