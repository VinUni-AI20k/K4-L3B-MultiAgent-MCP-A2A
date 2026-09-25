# L3B Architecture Record

## Luồng xử lý

```text
Input → Entity agent → Order agent → Payment agent → Shipment agent → Policy agent
          │             │              │                 │                │
          └─────────────┴──────────────┴─────────────────┴────────────────┘
                                      │ MCP evidence broker (case-scoped cache)
                                      ↓
                     Coordinator → Groq 7B model verifier → Deterministic verifier
                                      ↓
                              Output + observable trace
```

`src/student_agent/workflow.py` điều phối. `investigators.py` định nghĩa các specialist và evidence broker; `analysis.py` chuẩn hóa dữ kiện và chọn đúng purchase episode. Model chỉ nhận facts đã chuẩn hóa, không nhận lời nhắn khách hàng, API key hoặc nội dung thô từ MCP. Kết luận model chỉ được chấp nhận nếu nằm trong tập issue đã suy ra từ evidence; verifier xác định kiểm tra mọi quan hệ quan trọng trước khi finalize.

## Quyền và handoff

| Actor | Tool được gọi | Handoff |
| --- | --- | --- |
| Entity | `get_customer_history`, `get_order` | order phù hợp, candidate bị loại, customer context |
| Order | `get_order_items`, `get_product_context` | item, seller, tổng giá hàng và freight theo purchase episode |
| Payment | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | capture, refund, trạng thái payment theo episode |
| Shipment | `get_shipment_summary`, `get_sellers` | carrier/customer timeline, seller deadline, actor của event |
| Policy | `get_policy` | rule đúng `policy_version` và issue |
| Model verifier | Không gọi MCP | chọn trong candidate issue đã có evidence |
| Deterministic verifier | Không gọi MCP | kiểm tra schema, refs, entity, tiền, status và action |

Coordinator emit `task_assigned`; mỗi specialist emit `tool_result_consumed` sau khi dùng evidence và `handoff` sau khi hoàn thành. Trình tự mỗi case là `case_received` → specialist work → `verification_completed` → `case_finalized`. Trace không lưu chain-of-thought.

## Entity resolution và source conflict

`claimed_order_id` là một candidate, không được mặc nhiên xác nhận. Entity agent chỉ gọi `get_order` cho ID hợp lệ, đối chiếu order ID, purchase timestamp và customer ID với customer history, rồi xác định resolved, ambiguous hoặc not_found. ID không hợp lệ được loại bằng kiểm tra định dạng. Evidence broker chỉ cache trong một case; mọi tool call luôn mang `case_id` và evidence refs giữ nguyên.

Một order ID có thể xuất hiện ở nhiều mốc mua. Với item, payment, refund và shipment rows có timestamp, mỗi row được gán cho purchase gần nhất trước nó trong customer history. `get_order` xác định purchase episode đang điều tra. Dated confirmed payment events được ưu tiên so với tổng của các base rows không có timestamp; event shipment đã xác nhận và actor của nó được ưu tiên khi phân trách nhiệm. Output ghi `data_conflicts` khi order/shipment status bất đồng hoặc tổng base payment chứa dòng ngoài episode.

## Quy tắc nghiệp vụ và model

Payment phân biệt captured, mismatch, duplicate capture, refund pending và refund failed bằng timeline đã scope. Shipment so carrier handoff với seller deadline, ngày giao với estimate và actor đã xác nhận. Policy agent đọc rule theo issue để lấy case status, action, party và refund amount; refund được giới hạn bởi số captured chưa hoàn. Claim được đánh giá riêng và chỉ liên kết refs phù hợp domain. Confidence giảm khi thiếu nguồn thiết yếu, policy hoặc model không đồng thuận.

Groq OpenAI-compatible endpoint dùng `allam-2-7b` (7B), temperature 0 và JSON mode. Cấu hình lấy từ `.env`: `AGENT_BASE_URL`, `AGENT_MODEL`, `AGENT_MODEL_PARAMS_B`, `AGENT_API_KEY`. `AGENT_MODEL_PARAMS_B` bắt buộc lớn hơn 0 và không quá 10. Không ghi key vào source, trace, output hay submission.

## Failure, hiệu quả và reproducibility

Gateway chỉ gọi các tool đã discover, mỗi `(tool, arguments)` tối đa một lần trong một case. Tool không khả dụng được đánh dấu thiếu evidence; không tạo giá trị giả. Model được thử tối đa hai lần nếu trả kết quả không hợp lệ. Chạy tuần tự để giữ trace và audit dễ kiểm chứng; không cache refs qua case hoặc qua run.

`day09 run` tạo outputs và trace trong thư mục staging. Chỉ khi đủ 100 case và toàn bộ artifact pass contract, runner mới đưa chúng vào `outputs/` và `traces/`; artifact trước đó được lưu ở `.run-backups/`. Dùng `day09 validate-inputs`, `pytest`, `day09 validate`, rồi `day09 package --output dist/submission.zip`. Python >=3.11; dependency ranges theo `pyproject.toml`. Không dùng randomness trong logic xác định, model temperature 0.
