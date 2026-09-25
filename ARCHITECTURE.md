# L3B Architecture Record

## 1. System overview

```text
case input
  -> entity-agent -> customer-agent -> coordinator
                                   -> order-product-agent
                                   -> shipment-agent
                                   -> payment-agent (payment/refund)
                                   -> policy-agent
  -> conflict-resolver -> verifier -> schema-valid output
          |                  |
          +--- MCP evidence -+---- observable trace
```

`solve_case` is an evidence-first coordinator. It discovers the MCP tool names once per gateway, then calls only discovered tools. The coordinator resolves an order before granting downstream specialists an `order_id`; therefore shipment, payment, refund, and policy calls cannot accidentally be made against an unresolved candidate.

## 2. Agent ownership

| Actor | Input | Responsibility | Permitted MCP domain | Handoff |
| --- | --- | --- | --- | --- |
| entity-agent | claimed ID, candidates, customer hint | Read candidates and resolve/reject orders | order | entity resolution |
| customer-agent | customer hint and scope | Obtain related-order context only when requested | customer | customer context |
| coordinator | specialist outputs | Assign work, keep case-local evidence registry | none directly | bounded tasks |
| order-product-agent | one resolved order ID | Identify item and seller entities | item/product | item entities |
| shipment-agent | one resolved order ID | Assess shipment timeline and delivery state | shipment | shipment verdict |
| payment-agent | one resolved order ID | Reconcile capture and refund evidence | payment/refund | payment verdict/totals |
| policy-agent | resolved ID if present | Fetch policy evidence; never infer eligibility if unavailable | policy | policy availability |
| conflict-resolver | observed domain values | Mark unresolved cross-source conflict | none | conflict record |
| verifier | assembled output | Enforce conservative status, evidence linkage, and schema invariants | none | final result |

Each tool result is validated by `EvidenceGateway` against the evidence contract before it can enter the case evidence registry. The workflow only puts returned `evidence_ref` values into output or trace.

## 3. Entity resolution and A2A protocol

Every message is correlated by the input `case_id`; task payloads carry only the minimum needed identifier and returned evidence, never private reasoning. The entity agent examines a claimed candidate first, then the remaining candidates in input order. A candidate is resolved only when it matches the supplied `customer_unique_id_hint`; with no hint, exactly one successfully retrieved candidate may be provisionally resolved at lower confidence. Multiple matches are `ambiguous`; absent usable order evidence is `not_found`.

Downstream investigation starts only for exactly one resolved order. Handoffs are acyclic: entity/customer/specialists -> coordinator -> conflict resolver -> verifier. A specialist receives one task per domain, and all tool calls have at most two attempts. This avoids both fan-out on ambiguous candidates and retry loops.

## 4. Evidence and conflict lifecycle

Evidence is held only in memory for the current `solve_case` call. Successful MCP results emit `tool_result_consumed` with the returned reference. Failures produce no evidence reference and are represented by conservative output fields (`null`, `insufficient_evidence`, or `needs_investigation`).

The resolver compares independently observed status values from the order and shipment sources. If they disagree, it adds a `data_conflicts` entry with `selected_source: null`, changes shipment to `conflicting`, and suppresses a causal conclusion. It does not choose a source merely to complete a result. Financial fields are populated only from named numeric fields in payment/refund evidence. The workflow does not calculate a refund recommendation from missing policy; `recommended_refund_brl: 0.0` with no refund line means no amount has been authorized, not that evidence established a zero entitlement.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Observable result |
| --- | ---: | --- | --- |
| MCP timeout/error | 2 attempts | Do not synthesize evidence | specialist handoff `evidence_unavailable` |
| Tool absent from discovery | 0 calls | Skip guessed tool name | `tool_unavailable`; conservative output |
| Entity unavailable/ambiguous | candidate calls only | Do not call order-scoped domains | `not_found`/`ambiguous`, `needs_investigation` |
| Source conflict | 0 extra calls | Preserve both source labels | unresolved conflict record |
| Invalid evidence envelope | 0 use | Gateway rejects it | no evidence ref is emitted |

Tool discovery is cached on the gateway, so it occurs at most once per run connection. Evidence is never cached across cases. Calls are scoped to exact candidate IDs and, after resolution, one order ID only.

## 6. Verification invariants

Before returning, the verifier checks these invariants by construction:

- output has the L3B schema version and the original `case_id`;
- resolved order IDs are a subset of successfully evidenced candidates, while rejected IDs are remaining input candidates;
- all output evidence references were returned by the current case's MCP calls and were traced when consumed;
- shipment/payment fields are `insufficient_evidence`/`null` when no single order is resolved;
- a detected source conflict prevents a causal conclusion;
- totals are non-negative values from evidence only; no refund line or non-zero recommendation is fabricated;
- confidence remains within `[0, 1]`, and unresolved evidence yields `needs_investigation`;
- trace has task assignments, handoffs, policy decision, verification, and is finalized by the CLI.

## 7. Reproducibility

The project targets Python 3.11+ and pins runtime/dev dependency ranges in `pyproject.toml`. The workflow is deterministic: it preserves candidate input order, makes no model call, uses no random seed, and has a fixed retry limit of two. There is no specialist concurrency because MCP audit efficiency and deterministic trace ordering take precedence. Run local checks with `ruff check .` and `pytest -q`; do not place credentials, inputs, generated outputs, or traces under version control.