# L3B Multi-Agent Architecture Record

This document describes the architectural decisions, multi-agent orchestration design, and invariant guarantees implemented for the Day09 L3B Multi-Agent MCP + A2A competition.

---

## 1. System Overview

The system implements a hierarchical Multi-Agent architecture using the **Google Agent Development Kit (ADK)** and Google Gemini Flash Lite models, with Agent-to-Agent (A2A) structured message envelopes and Model Context Protocol (MCP) evidence gathering.

```text
                          ┌──────────────────────────┐
                          │   Coordinator / Router   │
                          └─────────────┬────────────┘
                                        │ (Handoff)
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
│ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
└────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ (MCP Evidence Collector)
                                        ▼
                               ┌──────────────────┐
                               │   Policy Agent   │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Verifier Agent  │
                               └────────┬─────────┘
                                        │ (Validated Output)
                                        ▼
                                   [END OUTPUT]
```

### Execution Flow:
1. **Case Ingestion**: `cli.py` emits `case_received` and routes the dispute to the `CoordinatorAgent`.
2. **Entity Resolution**: `Coordinator` inspects candidate IDs, rejects synthetic/noise candidates (`candidate-XXX`), and verifies customer order history via `get_customer_history`.
3. **Specialist Delegation**: `Coordinator` assigns bounded subtasks to three domain specialists via parallel asynchronous tasks:
   - `OrderAgent`: Catalog items, seller associations, product categories, and order fulfillment status.
   - `ShipmentAgent`: Carrier milestones, delivery vs estimated dates, and seller shipping limit deadlines.
   - `PaymentAgent`: Payment captures, installment schedules, reconciliation mismatches, and refund timelines.
4. **Policy & Conflict Resolution**: Specialist findings are collected and forwarded to `PolicyAgent`. Powered by Gemini Flash Lite (`gemini-3.5-flash-lite` / `gemini-flash-lite-latest`) with deterministic fallback, the Policy Agent cross-references evidence against `EC_POLICY_V2` rules and resolves buyer-seller-carrier conflicts.
5. **Independent Verification**: `VerifierAgent` audits hard invariants (schema compliance, financial math, chronological timelines, and 100% evidence provenance) before output generation.
6. **Finalization**: `cli.py` emits `case_finalized` and serializes the result into `outputs/<case_id>.json`.

---

## 2. Agent Ownership & Least Privilege

| Actor | Input | Responsibility | Tool Permissions | Output / Handoff |
| :--- | :--- | :--- | :--- | :--- |
| **Coordinator / Router** | Case metadata, raw names, candidates | Disambiguate customer & order IDs, assign tasks, prevent cycles | `get_customer_history` | Domain Specialists |
| **Order / Item Agent** | `order_id`, scope | Validate items, prices, seller IDs, catalog categories, financial reconciliation | `get_order`, `get_order_items`, `get_sellers`, `get_product_context` | `OrderFindings` / `order_output` -> Policy Agent |
| **Shipment Agent** | `order_id` | Audit carrier transit, handoff deadlines, identify late sellers | `get_shipment_summary` | `ShipmentFindings` -> Policy Agent |
| **Payment Agent** | `order_id` | Audit captures, duplicate charges, reconciliation mismatches, refunds | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `PaymentFindings` -> Policy Agent |
| **Policy Agent** | All specialist findings + case claims | Apply platform policy rules, resolve conflicting claims, determine refund | `get_policy` | `AdjudicationDraft` -> Verifier Agent |
| **Verifier Agent** | Draft resolution + case context | Audit invariants, financial math, schema validity, evidence provenance | None (pure verification) | Validated Final Output |

---

## 3. Entity Resolution & A2A Protocol

- **Candidate Disambiguation**: Genuine Olist orders have 32-character hexadecimal UUIDs. Synthetic candidate entries (e.g. `candidate-001`) are rejected and recorded in `entity_resolution.rejected_candidates`.
- **A2A Envelopes**: All messages between agents are encapsulated in `A2AMessage` envelopes containing `message_id`, `case_id`, `sender`, `recipient`, `intent`, `payload`, and `hop_count`.
- **Cycle Prevention**: Hard limit of `hop_count <= 10` prevents circular agent handoffs.
- **Trace Hygiene**: Observable trace events (`case_received`, `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`, `case_finalized`) record operational milestones without logging prompts, raw LLM reasoning, or secrets.

### 3.1. Order & Product Intelligence and Financial Grounding

- **Item Financial Reconciliation**:
  - `OrderProductAgent` aggregates:
    - `total_items_price = sum(item.price)`
    - `total_freight_value = sum(item.freight_value)`
    - `total_order_value = total_items_price + total_freight_value`
  - Provides independent item-level baseline for detecting `capture_mismatch` or `duplicate_charge`.
- **Multi-Seller Disaggregation**:
  - Tracks `seller_financials`: `{seller_id: {items: [...], item_price: float, freight: float, total: float}}`.
  - Enables `PolicyAgent` to ground seller delay freight refunds in the offending seller's actual freight rather than heuristic defaults.
- **Seller Shipping Deadlines**:
  - Extracts `shipping_limit_date` for each item to clarify whether delays were caused by seller handoff or logistics transit.
- **Cross-Domain Evidence Mapping**:
  - `get_evidence_for_topic` dynamically incorporates `item`, `product`, and `seller` evidence into respective claims (`late_delivery_seller`, `canceled_order_paid`, `unavailable_order_paid`), optimizing F1 evidence coverage.

---

## 4. Evidence Lifecycle & Conflict Resolution

- **Provenance Assurance**: Every cited `evidence_ref` in `outputs/<case_id>.json` and `claim_assessments` must have been returned by an MCP server tool call during that specific case execution.
- **Session-Scoped Caching**: `CaseEvidenceContext` caches tool responses by `(tool_name, arguments)` within the case scope, eliminating redundant roundtrips.
- **Conflict Handling**: When a customer's claim conflicts with authoritative telemetry (e.g. customer claims late delivery but tracking shows on-time delivery), the conflict is logged in `data_conflicts` with `selected_source: "carrier_telemetry"` and resolved in accordance with platform policy.

---

## 5. Failure & Efficiency Policy

| Failure Condition | Retry Budget | Fallback Strategy | Observable Trace |
| :--- | :---: | :--- | :--- |
| **MCP Tool Timeout** | 2 retries | Exponential backoff (0.5s, 1s) | Log warning attribute |
| **Tool Execution Error (e.g. Missing Refund)** | 0 retries | Treat as absence of refund records (empty list) | Graceful empty fallback |
| **LLM Output Parse Error** | 1 retry (fallback model) | Deterministic policy rule grounding | `policy_decided` |
| **Entity Ambiguity** | 0 retries | Reject synthetic candidates, rank by customer history | `entity_resolution` with calibrated confidence |

---

## 6. Verification Invariants

Before any case is finalized, `VerifierAgent` verifies:
1. **Schema Conformance**: 100% pass against `contracts/schemas/l3b-output-v2.schema.json`.
2. **Financial Invariant**: `recommended_refund_brl <= captured_total_brl`.
3. **Timeline Invariant**: `order_delivered_customer_date >= order_delivered_carrier_date >= order_purchase_timestamp`.
4. **Action Consistency**: If `recommended_refund_brl > 0`, `case_status == "action_required"`; if `0`, `case_status in ("no_action", "needs_investigation")`.
5. **Seller Accountability**: If `late_delivery_seller` or `unavailable_order_paid`, responsible party points to the verified seller ID.
6. **Confidence Calibration**: Expresses confidence $c \in [0.80, 0.98]$ reflecting evidentiary completeness; never returns $1.0$ on ambiguous cases.

---

## 7. Reproducibility

- **Runtime Environment**: Python 3.11+
- **Agent Framework**: Google Agent Development Kit (`google-adk >= 2.9.0`), `google-genai >= 2.25.0`
- **Model Configurations**: Primary `gemini-3.5-flash-lite`, Fallback `gemini-flash-lite-latest`
- **Deterministic Settings**: Temperature `0.0`, structured JSON mode (`response_schema=AdjudicationDraft`)
- **CLI Commands**:
  - Run: `day09 run`
  - Validate: `day09 validate`
  - Package: `day09 package --output dist/submission.zip`
