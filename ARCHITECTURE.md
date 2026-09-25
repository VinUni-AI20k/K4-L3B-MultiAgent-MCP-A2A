# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. MCP Gateway boundary

`EvidenceGateway.call()` is the only boundary used by specialist agents. It always sends the
current case ID, rejects an empty tool or case ID, accepts structured content or one JSON text
block, validates the complete `mcp-evidence-response-v1` envelope, and returns the server object
unchanged. The client never creates, hashes, normalizes, or substitutes `evidence_ref`.

`InvestigationContext.call()` owns the observable consumption step: it records the server-provided
reference in the case-local registry and emits exactly one `tool_result_consumed` event for each
uncached successful call. Failed calls produce no fabricated evidence or trace reference.

## 3. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Claimed/candidate order IDs và customer hint | Xác minh candidate, xếp hạng, hiệu chỉnh confidence | `get_order` | `EntityResolution` kèm evidence refs về Coordinator |
| Coordinator | `customer_request`, claims và entity result | Chuẩn hóa request, route worker, quản lý evidence cache, merge kết quả | Không gọi domain tool trực tiếp | Case plan và task assignment cho specialists |
| Order/product | Resolved order IDs | Preserve item/seller identity for output | `get_order_items` | Item and seller IDs to Coordinator |
| Shipment | Resolved order IDs | Compare promised and actual delivery dates; assign responsibility | `get_order`, `get_order_items`, `get_shipment` | `shipment_analysis` to Coordinator |
| Payment/refund | Resolved order IDs | Reconcile captured/refunded/refundable amounts and statuses | `get_payment`, `get_refund` | `payment_analysis` to Coordinator |
| Policy | Entity result, claims và kết quả shipment/payment nếu có | Gọi `get_policy`, kiểm tra thời hạn 7/30 ngày và đánh giá claim | `get_policy` | `PolicyAnalysis` về Coordinator |
| Conflict resolver | Specialist results and policy evidence | Detect conflicts and apply policy > payment > logistics precedence | None | `data_conflicts` and root-cause data to Verifier |
| Verifier | Complete merged result | Check schema-related invariants, evidence scope and confidence | None | Validated output and `verification_completed` |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Coordinator chuẩn hóa `claimed_order_id`, `candidate_order_ids`, customer hint và claims thành
`CasePlan`, sau đó emit `task_assigned` cho Entity Agent. Entity Agent chỉ gọi `get_order`
trong đúng `case_id`; candidate không tồn tại hoặc trả về order ID khác bị loại. Điểm nền của
candidate hợp lệ là 0.55, cộng 0.25 nếu trùng claimed order và 0.15 nếu customer hint khớp.
Một candidate duy nhất được resolve với confidence tối thiểu 0.75; nhiều candidate cần claimed
order hợp lệ hoặc margin tối thiểu 0.1. Nếu không đủ margin, kết quả là `ambiguous`, không đoán.
Kết quả được handoff về Coordinator bằng trace metadata, không ghi suy luận riêng.

## 4. Evidence và conflict lifecycle

`InvestigationContext` có vòng đời theo từng case, cache theo `(tool, arguments)`, thu duy nhất
`evidence_ref` do Gateway trả về và emit `tool_result_consumed`. Cache không được chia sẻ giữa
các case; cùng một tool call trong case chỉ gọi MCP một lần. `merge_worker_results` từ chối
specialist payload có field trùng nhau để tránh ghi đè kết quả âm thầm.

Policy Worker chỉ dùng policy evidence để quyết định eligibility. Kết quả Logistics và Financial
được truyền vào như ngữ cảnh để Coordinator hợp nhất, không bị Policy Worker gọi lại hoặc tự
ghi đè. Thiếu policy evidence, entity chưa resolve hoặc policy window không xác định đều giữ
trạng thái thiếu evidence; không suy đoán 7/30 ngày.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 0 retries | Keep `insufficient_evidence`; never invent data | Successful calls emit `tool_result_consumed` |
| Entity not found/ambiguous | 0 retries | Do not call downstream tools without a resolved order | `handoff` with entity status |
| Source conflict | 0 retries | Preserve conflict; apply policy > payment > logistics | `policy_decided` / verifier metadata |
| Invalid specialist result | 0 retries | Use schema-safe insufficient-evidence result | `verification_completed` after validation |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

Query budget is case-scoped and deterministic: candidate resolution calls `get_order` once per
candidate; specialists call domain tools only for the resolved order; `InvestigationContext`
caches identical `(tool, arguments)` calls. There is no broad scan and no retry, so missing
evidence cannot become invented data.

The verifier recalibrates confidence from evidence coverage, unresolved entity state and source
conflicts. It enforces responsibility consistency: seller-delay cases cannot assign the logistics
provider as the sole responsible party, and logistics-delay cases cannot assign the seller as the
sole responsible party. Refund lines must sum to the recommended BRL amount; unsupported or
insufficient-evidence cases cannot recommend a refund. Finalization occurs only after these
checks and `verification_completed`.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
