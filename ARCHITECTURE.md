# L3B Architecture Record

Tài liệu mô tả quyết định có thể kiểm chứng của workflow. Không ghi prompt bí mật hoặc
chain-of-thought vào output hay trace.

## 1. System overview

```text
Input ─► Entity agent (rule) ─► Coordinator evidence plan (MCP, song song, mỗi tool 1 lần)
                                   │
                                   ▼
                       scoped_evidence(): incident window, loại decoy
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
   order-agent (LLM)      shipment-agent (LLM)      payment-agent (LLM)     ← chạy song song
          └────────────────────────┼────────────────────────┘
                                   ▼
                     Coordinator (LLM) chọn primary_issue
                                   ▼
            Verifier (rule engine độc lập) ── bất đồng ─► Coordinator xem lại 1 lần
                                   ▼
          analyze_case(issue_override): policy, refund, entities ─► hard checks ─► Output
MCP evidence ──► case-local ledger ──► tool_result_consumed ──► Trace
```

LLM quyết định vấn đề (issue). Phần tính toán tài chính, policy mapping và entity được suy
ra cơ học từ issue đó bằng code, để tránh LLM bịa số tiền hoặc evidence ref.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case, customer hint, claimed order | Xác nhận claimed order có trong history của khách; reject `candidate-*` | `get_customer_history`, `get_order` | `ENTITY_RESOLVED` / `ENTITY_NOT_FOUND` |
| Coordinator | brief, findings, scoped evidence | Lập evidence plan; LLM chọn một `primary_issue` | Không gọi tool trực tiếp | `policy_decided` |
| Order/product | incident order, items, payments | LLM: trạng thái canceled/unavailable, seller, item | `get_order_items` | finding `{issue, confidence}` |
| Shipment | incident order, items, shipment events, shipping limits | LLM: trễ do seller hay logistics | `get_shipment_summary` | finding |
| Payment/refund | payments, payment/refund events | LLM: duplicate, mismatch, split, refund pending/failed | `get_payment_timeline`, `get_refund_timeline` | finding |
| Policy | policy version | Nạp rules theo issue | `get_policy` | `POLICY_LOADED` |
| Conflict resolver | history vs order vs shipment | Chọn incident window trước `opened_at`, ghi `data_conflicts` | Không gọi tool | trong `scoped_evidence` / `analyze_case` |
| Verifier | output ứng viên | Rule engine phản biện issue; hard checks schema/refs/tiền | Không gọi tool | `verification_completed` |

Argument của mọi tool do code cố định: `case_id`, customer hint, `policy_version` và
order đã resolve. LLM không bao giờ nhìn thấy hoặc cung cấp `case_id` hay `evidence_ref`.

## 3. Entity resolution và A2A protocol

- Claimed order chỉ được chấp nhận khi có trong `get_customer_history`, còn ID dạng
  `candidate-*` bị loại. Không resolve được thì không gọi các tool theo order, và output sẽ là
  `insufficient_evidence`.
- Order có nhiều snapshot thì chọn dòng có purchase timestamp muộn nhất nhưng không sau
  `opened_at`, bỏ qua snapshot trùng với `get_order` (decoy). Cửa sổ incident kéo dài tới lần
  mua kế tiếp.
- Mọi message được correlation bằng `case_id`, handoff là `task_assigned` → `handoff`.
  Không có vòng lặp agent tự do: specialist gọi LLM 1 lần, coordinator tối đa 2 lần (thêm 1
  lần xem lại khi verifier phản đối).

## 4. Evidence và conflict lifecycle

`EvidenceGateway` validate mọi MCP envelope theo contract. `_Case` là ledger trong phạm vi
case: mỗi tool gọi tối đa 1 lần, kể cả khi lỗi (không retry call đã bị audit). Mỗi response
dùng được sẽ emit `tool_result_consumed` với `evidence_ref` nguyên bản do actor sở hữu tool
ghi. Output chỉ chứa refs có trong ledger (verifier kiểm tra `REFS`). Không chia sẻ evidence
giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/tool error | 0 | Ghi `None` trong ledger; specialist thấy trong `missing_tools` | `handoff/TOOL_CALL_FAILED` |
| Entity not found/ambiguous | 0 | Không gọi tool theo order; `insufficient_evidence` | `ENTITY_NOT_FOUND` |
| Source conflict | 0 | Chọn incident window theo `opened_at`; ghi `data_conflicts` | `verification_completed` |
| Invalid specialist result | 0 | Finding `None`; coordinator vẫn chạy | `handoff/NO_FINDING` |
| LLM 429/5xx | 2 (backoff 1s, 2s) | Hết lượt: issue `insufficient_evidence` | `ISSUE_MISMATCH` |
| Lỗi bất ngờ trong case | 0 | Output rule-based rỗng evidence cho case đó | stderr `WARN` |

Budget MCP: 6 call/case (`history`, `order`, `items`, `shipment`, `payment_timeline`,
`policy`), thêm `refund_timeline` khi claim hoặc payment event có refund. Hard cap 8 call.
Không gọi `get_product_context`, `get_order_payments`, `get_sellers` vì output không cần.
Budget LLM: 4 call/case (5 khi verifier phản đối).

## 6. Verification invariants

Output validate đúng JSON Schema. Refs nằm trong tập đã consume. `no_action` không có refund.
Tổng refund lines bằng `recommended_refund_brl`. Action không trùng. Resolved và rejected
candidates không giao nhau. Confidence được hiệu chỉnh: LLM đồng ý với rule engine thì
0.8–0.95 (cộng thêm theo số specialist cùng kết luận), bất đồng thì tối đa 0.6. CLI validate
schema lần nữa trước khi ghi file.

## 7. Reproducibility

- Python ≥ 3.11, dependency pin trong `uv.lock`.
- LLM: endpoint OpenAI-compatible (`OPENAI_BASE_URL`, `OPENAI_MODEL`), `temperature=0`,
  `response_format=json_object`. Model phải có **< 10 tỷ tham số**.
- Concurrency: `DAY09_CONCURRENCY` case song song (mặc định 4); trong một case, các MCP call
  và 3 specialist chạy song song.
- Lệnh: `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`.
  `DAY09_CASES=L3B_CASE_001,...` chạy một phần; `DAY09_RESUME=1` bỏ qua case đã finalize.
- API key chỉ đọc từ `.env`.
