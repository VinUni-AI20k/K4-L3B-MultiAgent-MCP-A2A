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
| Entity/customer | Claimed/candidate order IDs và customer hint | Xác minh candidate, xếp hạng, hiệu chỉnh confidence | `get_order` | `EntityResolution` kèm evidence refs về Coordinator |
| Coordinator | `customer_request`, claims và entity result | Chuẩn hóa request, route worker, quản lý evidence cache, merge kết quả | Không gọi domain tool trực tiếp | Case plan và task assignment cho specialists |
| Order/product | TODO | TODO | TODO | TODO |
| Shipment | TODO | TODO | TODO | TODO |
| Payment/refund | TODO | TODO | TODO | TODO |
| Policy | TODO | TODO | TODO | TODO |
| Conflict resolver | TODO | TODO | TODO | TODO |
| Verifier | TODO | TODO | TODO | TODO |

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

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Entity not found/ambiguous | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
