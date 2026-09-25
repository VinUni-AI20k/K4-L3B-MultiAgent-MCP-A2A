# L3B Architecture Record

This document describes the implemented L3B v1 solver. The official input set
contains 100 cases and is validated by `day09 validate-inputs`. Each case has a
claimed order, one distractor candidate, a customer hint, two claims, and a
policy version. The MCP tool inventory was discovered against the gateway.

## 1. System overview

```text
Case input
  → coordinator identifies questions and candidate entities
  → MCP tool discovery confirms the expected tool inventory
  → coordinator calls scoped evidence tools for the case
  → specialists interpret order/customer, shipment, payment/refund and policy evidence
  → conflict resolver records disagreements and selected sources
  → verifier checks evidence linkage and cross-field consistency
  → output JSON + observable trace
```

The Python workflow is the coordinator. Specialist responsibilities are logical
roles and do not require separate model instances or an agent framework. The
MCP gateway is a data/tool boundary, not an agent: it transports calls and
returns validated evidence envelopes. The workflow calls `get_order`,
`get_customer_history`, `get_order_items`, `get_shipment_summary`,
`get_payment_timeline`, `get_product_context`, and `get_policy`, plus
`get_refund_timeline` for the four input topics where the current case set
returns refund evidence. The CLI checks that the gateway has tools before running. This
implementation does not create separate model processes for the specialists.

## 2. Agent ownership

| Actor | Input | Responsibility | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Case narrative and candidates | Resolve customer/order only from matching evidence; preserve ambiguity | Coordinator-mediated, case-scoped MCP calls | Candidate disposition and evidence refs |
| Coordinator | Case and specialist questions | Discover tools, assign bounded work, enforce case scope and call budget, assemble result | Calls only discovered tools with schema-valid arguments | Specialist tasks and assembled draft |
| Order/product | Resolved candidate IDs | Confirm order/item/product/seller facts relevant to the complaint | Coordinator-mediated calls for required domains | Facts and supporting evidence refs |
| Shipment | Resolved order/shipment IDs | Reconstruct event timeline and assess delay/responsibility | Coordinator-mediated shipment calls | Verdict, timeline completeness, evidence refs |
| Payment/refund | Resolved order/payment IDs | Reconcile captures and refunds; distinguish pending, failed and completed refunds | Coordinator-mediated payment/refund calls | Totals, verdict and evidence refs |
| Policy | Relevant facts and public policy evidence | Apply retrieved policy to supported facts | Coordinator-mediated policy calls when available | Policy decision and evidence refs |
| Conflict resolver | Conflicting source facts | Record source disagreement; select a source only when an explicit precedence rule supports it | No independent calls; requests missing evidence through coordinator | Conflict record or unresolved status |
| Verifier | Draft output and evidence ledger | Check schema, scope, claim linkage and cross-field invariants | No independent calls by default | Verification result and correction requests |

Only the coordinator holds the gateway connection. Specialist actor names in
trace events identify ownership of analysis, while the coordinator's scoped
`fetch` helper makes the actual calls.

## 3. Entity resolution and A2A protocol

The current input set supplies a claimed order ID in the candidate list. The
workflow confirms that ID with `get_order`, then rejects the distractor. If
the lookup cannot confirm the ID, it reports an unresolved entity instead of
using the claim as fact. This approach is tailored to the v1 input set; it does
not search arbitrary candidate lists when the claimed order lookup fails.

Entity confidence is a fixed heuristic (0.98 on confirmation, 0.25 otherwise).
Assessment confidence is also heuristic (0.93 for a supported inferred issue,
0.85 for an unsupported claim, 0.35 for insufficient evidence). These values
have not been calibrated against private feedback.

Each specialist task is scoped to one `case_id`, has a clear question and
returns a compact result with evidence refs. The coordinator records observable
`task_assigned` and `handoff` events. A specialist must not call tools for a
different case or start an unbounded investigation. No separate inter-agent
wire protocol is needed for the starter implementation; Python data objects
carry these handoffs.

## 4. Evidence and conflict lifecycle

1. Discover tool definitions from the MCP server and verify the expected tools
   are available before solving the case set.
2. Call a tool with the current `case_id`. Validate the response against the
   public evidence envelope schema.
3. Keep the returned `evidence_ref` unchanged. Record
   `tool_result_consumed` when a result is used, including the tool name and
   evidence ref.
4. Link output claims to evidence refs. Do not reuse refs across cases.
5. For conflicting values, preserve the disagreement in `data_conflicts`.
   When an order ID has multiple purchase snapshots, select the latest one
   purchased before the case opened; the `get_order` row may describe a
   different snapshot. Prefer matched lifecycle events to base payment rows,
   and item rows closest to the selected purchase date.

For a canceled or unavailable claim, a history snapshot with that exact status
is preferred when it existed before case open. This only chooses among
conflicting rows for the same confirmed order; payment evidence must still
confirm that the order was charged.

Payment capture events are matched within three days of the selected order's
purchase/approval timestamp. Shipment late events are matched within 30 days
of its delivery; refund events outside the period from purchase to case open
are ignored.
If a third capture shares the same timestamp, the solver can select a group of
two or more captures that reconciles exactly to item price plus freight. A
refund tied to a capture excluded by either the date or amount match is
excluded from that snapshot as well. Identical capture rows with the same
timestamp on a canceled or unavailable order are counted once.
These bounds protect against stale duplicate rows observed in the gateway's
responses, but may need revision if the case set changes.

The gateway caches successful identical calls only within the same case. The
cache key includes case ID, tool name and canonicalized arguments. Failures are
not cached. The cache does not replace trace events: the workflow must record
evidence when it consumes a result.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout or tool error | 0 automatic retries within a case | Tool errors yield missing evidence; a connection failure stops the batch and `build --resume` continues with a new connection | No invented result |
| Entity not found/ambiguous | 0 | Preserve `not_found` or `ambiguous`; request more evidence only if budget allows | `entity_not_found` / `entity_ambiguous` decision code |
| Source conflict | 0 | Apply documented source precedence or leave unresolved | `source_conflict_unresolved` |
| Invalid specialist result | 0 | Reject it and assemble a schema-valid insufficient-evidence result only when case facts support that status | `specialist_result_invalid` |

No automatic per-tool retry is enabled. Identical successful calls are cached
within a case. The workflow calls seven or eight scoped tools per case,
depending on whether refund evidence is requested. A failed batch can resume
completed cases without repeating their MCP calls. The CLI accepts
`--workers N` to process cases concurrently and compacts incomplete trace
segments after a successful resumed run.
There is no cross-case evidence cache. All actual calls, including exploratory
ones, remain subject to the server's audit and efficiency scoring.

## 6. Verification invariants

Before finalizing a case, verify:

- output conforms to the L3B schema and carries the current `case_id`;
- resolved and rejected entities are supported by evidence, with ambiguity
  retained when resolution is not unique;
- every output evidence ref came from an MCP response for this case and every
  consumed result is linked in the trace;
- claim assessments, root causes and actions do not exceed what the evidence
  supports;
- shipment verdict agrees with the observed timeline and responsibility;
- captured, refunded and refundable totals are non-negative and internally
  consistent;
- payment/refund status, case status and recommended actions agree;
- conflicts identify multiple sources and do not claim a selected source when
  none is justified;
- confidence is bounded from 0 to 1 and reflects evidence strength.

## 7. Reproducibility

- Runtime: Python 3.11 or newer.
- Dependencies: declared in `pyproject.toml`; install the project and dev extras
  from that file.
- Model: none is configured by the starter kit. The coordinator is currently
  ordinary Python code; do not claim model-based reasoning unless one is added
  and recorded here.
- Randomness: trace event IDs use secure random tokens; solver decisions should
  be deterministic for identical inputs and tool results.
- Commands: `day09 validate-inputs`, `day09 mcp-tools`,
  `day09 run --limit 7 --progress`, `day09 validate-sample --limit 7`,
  `day09 build --workers 4 --progress`, and
  `day09 build --resume --workers 4 --progress`.
- Secrets: keep the team API key in `.env`; never include it in source, traces,
  outputs or the submission archive.

The private oracle and case-level score reports are unavailable. Local
validation checks schema and trace integrity; it cannot establish semantic
correctness against the private oracle.
