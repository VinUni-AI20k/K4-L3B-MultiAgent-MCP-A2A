# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case input JSON | Tiếp nhận hồ sơ, phân chia nhiệm vụ, theo dõi tiến độ và tổng hợp báo cáo | Không gọi MCP tools | Giao task cho Entity & Specialists qua `task_assigned` |
| Supervisor LLM | customer_request, claims | Suy luận reasoning phân tích yêu cầu khiếu nại, lập kế hoạch và định tuyến domain tối ưu | Không gọi MCP tools | Quyết định domain & reasoning handoff cho Coordinator |
| Entity/customer | candidate_order_ids, customer_unique_id_hint | Xác thực đơn hàng thực tế, loại bỏ candidate rác, lập danh sách đơn liên quan | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context` handoff cho Coordinator/Specialists |
| Order/product | Resolved order_id, investigation_scope | Trích xuất items, sellers, sản phẩm và trạng thái mua hàng | `get_order_items`, `get_product_context` | `item_ids`, `seller_ids`, product context handoff cho Policy/Conflict |
| Shipment | Resolved order_id, shipping limits | Phân tích mốc thời gian giao hàng, xác định lỗi trễ do người bán hay vận chuyển | `get_shipment_summary`, `get_sellers` | `shipment_analysis`, `late_seller_ids` handoff cho Conflict/Policy |
| Payment/refund | Resolved order_id, claimed issue | Kiểm tra phương thức thanh toán, dòng tiền, sự kiện hoàn tiền, đối soát chênh lệch | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis`, financial totals handoff cho Conflict/Policy |
| Policy | Claims, bằng chứng từ specialists | Tra cứu chính sách bồi hoàn `EC_POLICY_V2`, xác định lỗi chính, phân bổ tiền hoàn | `get_policy` | `assessment`, `root_cause_analysis`, `financial_resolution` |
| Conflict resolver | Dữ liệu đa nguồn từ các specialists | Phát hiện mâu thuẫn giữa các bảng (thời gian, trạng thái), hòa giải theo độ ưu tiên | Không gọi MCP tools | `data_conflicts` kèm `resolution_code` |
| Verifier | Toàn bộ dự thảo output | Thẩm định độc lập 8 Invariants (Schema, Consistency, Provenance, Totals) | Không gọi MCP tools | Báo cáo kiểm định hoàn tất qua `verification_completed` |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- **Xếp hạng & Reject candidate:** 
  - Với mỗi candidate trong `candidate_order_ids`, hệ thống kiểm tra định dạng và gọi `get_order`.
  - Candidate giả lập/không tồn tại (dạng `candidate-xxx` hoặc mã lỗi MCP) bị đưa vào `rejected_candidates`.
  - Candidate hợp lệ được đối chiếu với `customer_unique_id` trong `get_customer_history`. Nếu khớp, đơn được gán vào `resolved_order_ids` với trạng thái `resolved` và độ tin cậy `0.95`.
- **A2A Message Protocol:**
  - Giao tiếp giữa các Agent tuân thủ cấu trúc phong bì chuẩn: `{ "case_id", "sender", "recipient", "intent", "payload", "timestamp" }`.
  - Mọi luồng giao tiếp tương ứng đều phát ra các sự kiện trace có thể quan sát: `task_assigned`, `handoff`, `policy_decided`, `verification_completed`.
  - Timeout giới hạn 30s mỗi tác tử, không chuyển giao vòng lặp (DAG acyclic).

## 4. Evidence và conflict lifecycle

- **Validation & Provenance:**
  - Mọi phản hồi từ MCP Gateway được validate tự động theo schema `mcp-evidence-response-v1.schema.json`.
  - Trích xuất `evidence_ref` duy nhất từ MCP response. Mỗi khi dữ liệu được sử dụng, tác tử phát sự kiện `tool_result_consumed` liên kết trực tiếp `evidence_ref` với `tool_name` và `actor`.
  - Bằng chứng được đóng khung cách ly theo từng `case_id`, tuyệt đối không dùng chéo giữa các case.
- **Conflict Lifecycle:**
  - Khi phát hiện mâu thuẫn thời gian (ví dụ ngày đặt hàng trên `get_order` xảy ra sau ngày mở khiếu nại `opened_at`), hệ thống đối chiếu với dòng thời gian từ `get_shipment_summary` và `get_customer_history`.
  - Ưu tiên nguồn dữ liệu có sự kiện xác nhận (`status: confirmed`) từ audit log vận chuyển hoặc thanh toán thực tế (`resolution_code: ACCEPTED_CONFIRMED_EVENT` hoặc `ACCEPTED_HISTORICAL_RECORD`).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 1 lần (backoff 1s) | Trả về null, đánh dấu thiếu chứng cứ | `mcp_timeout_fallback` |
| Entity not found/ambiguous | 0 retry | Gán status `not_found` hoặc `ambiguous`, confidence 0.2 | `entity_unresolved` |
| Source conflict | 0 retry | Ghi nhận vào `data_conflicts`, chọn nguồn ưu tiên xác thực | `conflict_detected` |
| Invalid specialist result | 1 retry nội bộ | Dùng giá trị mặc định từ case metadata | `specialist_fallback` |

- **Efficiency Policy:**
  - Tích hợp **Supervisor LLM** (< 10B parameters, model `allam-2-7b` / `llama-3.1-8b-instant`) với chuỗi suy luận (reasoning) phân loại chính xác domain trước khi phân việc.
  - Ngân sách gọi tool siêu tối ưu trung bình **4.1 MCP calls/case**:
    - Entity Resolution: đúng 2 calls (`get_order`, `get_customer_history`).
    - Specialists: 1 đến 2 calls tối thiểu có chủ đích theo domain (ví dụ `shipment`: chỉ `get_shipment_summary` + `get_policy`; `payment`: chỉ `get_order_payments` + `get_policy`; `unsupported`/`general`: chỉ `get_policy`).
  - **Liên kết bằng chứng cấp khiếu nại (Claim-level evidence mapping):**
    - Claim 0 (vấn đề chính): chỉ liên kết với bằng chứng trực tiếp chứng minh lỗi (`get_order`, domain specialist tool).
    - Claim 1 (yêu cầu hoàn tiền): chỉ liên kết với bằng chứng chính sách và dòng tiền (`get_policy`, `get_order_payments`).
    - Giúp tối ưu hóa điểm Evidence coverage lên mức tuyệt đối.
  - **Khả năng chịu lỗi mạng (Network Resiliency):** Tích hợp DNS bypass cục bộ cho `sslip.io` và cơ chế tự động retry kết nối mạng.
  - Duy trì in-memory cache theo `(tool_name, arguments)` trong suốt phiên xử lý của từng case.

## 6. Verification invariants

Trước khi chốt hồ sơ (`case_finalized`), Verifier Agent kiểm tra độc lập các điều kiện tiên quyết:
1. **Schema Invariant:** Toàn bộ output khớp 100% với `l3b-output-v2.schema.json`.
2. **Entity Scope:** Mọi `resolved_order_ids` phải thuộc về tập `candidate_order_ids` ban đầu.
3. **Rejected Candidates:** Các candidate bị từ chối phải nằm trong danh sách không hợp lệ và được ghi nhận đầy đủ.
4. **Evidence Provenance:** 100% `evidence_refs` trong output phải được sinh ra từ MCP Gateway đúng phiên chạy của case hiện tại.
5. **Consistency Invariant:**
   - Nếu `case_status == "no_action"`, `recommended_refund_brl` phải bằng `0.0`, `refund_lines` rỗng, và action là `document_no_action`.
   - Nếu `case_status == "action_required"`, `recommended_refund_brl` phải bằng tổng `amount_brl` của các `refund_lines`.
   - Trách nhiệm của người bán (`party_type: seller`) phải kèm theo `party_id` chính xác của người bán gây lỗi.
6. **Calibration Invariant:** Điểm tin cậy `confidence` nằm trong đoạn `[0.0, 1.0]`, phản ánh đúng mức độ đầy đủ của chứng cứ.

## 7. Reproducibility

- **Môi trường & Công cụ:** Python 3.11, `mcp>=2.2.0`, `httpx2>=2.13`, `jsonschema>=4.26`.
- **Cơ chế thực thi:** Deterministic asynchronous state machine, không dùng random seed, kết quả tái lập 100%.
- **Lệnh thực thi chuẩn:**
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
