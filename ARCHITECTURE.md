# L3B Architecture Record

Tài liệu này mô tả kiến trúc có thể kiểm chứng của workflow L3B. Tài liệu không chứa API key,
prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý: `Input → Entity Resolver → Coordinator → Specialist Agents → Conflict Resolver → Verifier → Output`.
MCP Evidence Gateway cung cấp evidence có audit; Trace Audit ghi nhận các sự kiện quan sát được.

Coordinator chuẩn hóa input, phân công specialist và hợp nhất kết quả. Specialist chỉ truy vấn MCP
trong phạm vi case hiện tại. Verifier kiểm tra output trước khi CLI ghi file kết quả.

## 2. MCP Gateway boundary

`EvidenceGateway.call()` là boundary duy nhất để gọi MCP. Mọi request đều truyền đúng `case_id`,
kiểm tra tool name/case ID không rỗng và xác thực envelope theo `mcp-evidence-response-v1`.

Gateway trả nguyên object do server trả về. Client không được tự tạo, hash, chuẩn hóa, thay thế
hoặc sửa `evidence_ref`.

`InvestigationContext.call()` quản lý evidence trong phạm vi một case. Với mỗi MCP call thành công
chưa được cache, context lưu nguyên `evidence_ref` và emit đúng một event `tool_result_consumed`.
MCP call thất bại không tạo evidence hoặc trace reference giả.

## 3. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity Agent | Claimed/candidate order IDs, customer hint | Xác minh candidate, xếp hạng và tính confidence | `get_order` | `EntityResolution` |
| Coordinator | Request, claims, entity result | Chuẩn hóa, route worker, cache và merge | Không gọi domain tool | Case plan, task assignment |
| Order/Item Agent | Resolved order IDs | Lấy item/seller identity | `get_order_items` | Item/seller data |
| Shipment Agent | Resolved order IDs | So sánh ngày hẹn/thực tế và phân định trách nhiệm | `get_order`, `get_order_items`, `get_shipment_summary` | `shipment_analysis` |
| Payment Agent | Resolved order IDs | Đối soát payment/refund | `get_order_payments`, `get_refund_timeline` | `payment_analysis` |
| Policy Agent | Entity, claims, shipment/payment results | Kiểm tra policy, thời hạn 7/30 ngày và claim | `get_policy` | `PolicyAnalysis` |
| Conflict Resolver | Specialist results, policy evidence | Phát hiện conflict và áp dụng precedence | Không gọi MCP | `data_conflicts`, root cause |
| Verifier | Merged result | Kiểm tra schema, evidence, consistency, confidence | Không gọi MCP | Validated output |

Áp dụng least privilege: tool discovery không có nghĩa mọi agent được gọi mọi tool.

## 4. Entity resolution and A2A protocol

Coordinator chuẩn hóa `claimed_order_id`, `candidate_order_ids`, customer hint, policy version và
claims thành `CasePlan`. Entity Agent emit `task_assigned`, gọi `get_order` một lần cho mỗi
candidate và chỉ chấp nhận evidence thuộc đúng case.

Candidate hợp lệ bắt đầu với confidence 0.55; cộng 0.25 nếu khớp claimed order và 0.15 nếu khớp
customer hint. Một candidate duy nhất được resolve với confidence tối thiểu 0.75. Nhiều candidate
chỉ được resolve khi claimed order hợp lệ hoặc margin tối thiểu 0.1. Nếu thiếu bằng chứng, kết quả
là `ambiguous` hoặc `not_found`, không đoán.

Handoff chỉ truyền metadata quan sát được: actor, target, decision code, confidence và evidence
refs. Không ghi suy luận nội bộ vào trace.

## 5. Evidence and conflict lifecycle

`InvestigationContext` có vòng đời theo từng case và cache theo `(tool, arguments)`. Cache không
được chia sẻ giữa các case; cùng một tool call trong một case chỉ gọi MCP một lần.

Evidence flow:

1. Agent nhận task từ Coordinator.
2. Agent gọi MCP qua Gateway với đúng `case_id`.
3. Gateway validate envelope và trả evidence nguyên bản.
4. Context lưu `evidence_ref` và emit `tool_result_consumed`.
5. Agent handoff kết quả cùng evidence refs về Coordinator.
6. Conflict Resolver áp dụng precedence: policy > payment > logistics.

Policy Worker dùng policy evidence để quyết định eligibility. Kết quả Shipment và Payment chỉ là
ngữ cảnh, không bị Policy Worker gọi lại hoặc ghi đè. Thiếu policy evidence, entity chưa resolve
hoặc policy window không xác định đều giữ trạng thái thiếu evidence.

## 6. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/error | 0 retry | Giữ `insufficient_evidence`, không bịa dữ liệu | Chỉ trace call thành công |
| Entity not found/ambiguous | 0 retry | Không gọi domain worker khi chưa resolve order | `handoff` với entity status |
| Source conflict | 0 retry | Giữ conflict, áp dụng policy > payment > logistics | `policy_decided`, verifier metadata |
| Invalid specialist result | 0 retry | Dùng result thiếu evidence theo schema | `verification_completed` |

Query budget theo từng case và deterministic. Candidate resolution gọi `get_order` một lần mỗi
candidate; specialist chỉ gọi domain tool cho order đã resolve. Cache loại bỏ call trùng, không
quét rộng và không retry tự động. Missing evidence không biến thành dữ liệu phỏng đoán.

## 7. Verification invariants

Trước khi finalize, Verifier kiểm tra:

- Output đúng public schema và không có field ngoài schema.
- `case_id` khớp input, filename và evidence/trace cùng case.
- Candidate bị loại không xuất hiện như resolved entity.
- Mọi evidence ref đến từ MCP server và có `tool_result_consumed` tương ứng.
- Claim được liên kết với policy evidence.
- Shipment timeline, payment totals và refund totals nhất quán.
- Precedence policy > payment > logistics được áp dụng khi có conflict.
- Seller delay không gán logistics là bên chịu trách nhiệm duy nhất; logistics delay không gán seller
  là bên chịu trách nhiệm duy nhất.
- `refund_lines` khớp `recommended_refund_brl`.
- Case thiếu evidence không được đề xuất refund hoặc kết luận quá mức.
- Confidence nằm trong [0, 1], giảm khi entity chưa resolve hoặc nguồn dữ liệu mâu thuẫn.
- Lifecycle trace có thứ tự: `case_received` → `task_assigned` → `tool_result_consumed` →
  `handoff` → `policy_decided` → `verification_completed` → `case_finalized`.
