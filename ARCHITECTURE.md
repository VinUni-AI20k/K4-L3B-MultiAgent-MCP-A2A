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

Phase 2 implementation uses a bounded handoff:
`coordinator -> order-item-agent -> shipment/payment/policy workers -> verifier`.
Workers return state patches; evidence and errors are append-only fields, so a
later worker cannot discard earlier audit data. Each consumed MCP response is
linked through its server-issued `evidence_ref` in output and trace.

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Order/item | Case IDs/candidates | Resolve order and item scope | Discovered order tools | IDs and confidence |
| Coordinator | Shared state | Bounded assignment/handoff | None | Worker target |
| Shipment | Resolved order | Collect shipment facts | Discovered shipment tools | Evidence |
| Payment/refund | Resolved order | Collect payment facts | Discovered payment tools | Evidence |
| Policy | Resolved order | Collect policy facts | Discovered policy tools | Evidence |
| Verifier | State/evidence refs | Build safe final output | None | Schema-valid output |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Mô tả cách xếp hạng/reject candidate, confidence threshold, message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Không trace nội dung suy luận riêng.

## 4. Evidence và conflict lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, chọn source theo policy, biểu diễn unresolved conflict, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Entity not found/ambiguous | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

The workflow caps handoff iterations at five. A failed MCP call is recorded and
stops specialist expansion; the verifier returns `needs_investigation` with
`insufficient_evidence`, no invented refund, and only real MCP evidence refs.
Every selected tool must first appear in MCP discovery and is called once at most
per case.

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 6a. Concrete A2A protocol and evidence lifecycle

The coordinator carries the original `case_id` in every handoff and MCP call.
The order/item agent prefers an explicit `order_id`; otherwise it uses the first
candidate supplied by the case and records remaining candidates as rejected. A
confirmed MCP order response gives 0.90 entity confidence; an unverified
supplied ID is only 0.50. No resolved order skips specialists and goes directly
to verification.

The observable protocol is `task_assigned`, `handoff`,
`tool_result_consumed`, then `verification_completed`. Private reasoning and
raw prompts are never written to trace. The counter increases at each worker
and is capped at five to prevent routing loops.

`EvidenceGateway` validates each MCP envelope against
`mcp-evidence-response-v1.schema.json`. The workflow preserves exactly the
server-issued `evidence_ref`, emits it in `tool_result_consumed`, and includes
only collected refs in the final output. Evidence is state-local and never
reused across cases. When a required domain is missing or sources conflict, the
verifier returns `insufficient_evidence`, recommends no refund, and routes the
case for manual review.

Retry is limited to one retry (two total calls) for an idempotent MCP evidence
read. A second failure records the worker error and stops specialist expansion;
the verifier still returns a schema-valid fallback output.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
