---
name: multiagent-a2a-orchestration
description: Use when designing, implementing, or debugging multi-agent systems with Coordinator and Specialist patterns, Agent-to-Agent (A2A) protocols, entity resolution, evidence verification, dispute investigation, or structured trace event emission.
---

# Multi-Agent A2A Orchestration

Comprehensive guide for designing, implementing, and verifying production-grade Multi-Agent systems using Coordinator-Specialist hierarchies, Agent-to-Agent (A2A) message protocols, evidence provenance, and observable trace auditing.

## When to Use This Skill

- Building multi-agent systems where tasks must be partitioned among domain specialists (Coordinator, Entity Resolver, Order/Product, Shipment, Payment/Refund, Policy, Verifier).
- Implementing A2A communication protocols with structured message envelopes and loop prevention.
- Investigating customer disputes, fraud, or e-commerce claims requiring cross-domain evidence correlation.
- Enforcing least-privilege tool execution and preventing cross-case evidence contamination.
- Emitting observable trace events (`task_assigned`, `handoff`, `tool_result_consumed`, `verification_completed`) for grading or auditing.

---

## 1. System Architecture: Coordinator-Specialist Hierarchy

```text
Input (Case / Complaint)
          │
          ▼
┌──────────────────┐
│ Entity Resolver  │ ◄─── MCP Tools: resolve candidates, lookup customer/order
└─────────┬────────┘
          ▼
┌──────────────────┐
│   Coordinator    │ ◄─── Orchestrates sub-tasks, assigns budgets, enforces timeouts
└─────────┬────────┘
          │
    ┌─────┴────────────────────────┬─────────────────────────────┐
    ▼                              ▼                             ▼
┌──────────────┐          ┌─────────────────┐          ┌───────────────────┐
│ Order Agent  │          │ Shipment Agent  │          │ Payment Agent     │
└──────┬───────┘          └────────┬────────┘          └─────────┬─────────┘
       │                           │                             │
       └───────────────────────────┼─────────────────────────────┘
                                   ▼
                       ┌───────────────────────┐
                       │ Conflict Resolver     │ ◄─── Reconciles claims vs evidence
                       └───────────┬───────────┘
                                   ▼
                       ┌───────────────────────┐
                       │   Verifier Agent      │ ◄─── Schema, bounds & invariant check
                       └───────────┬───────────┘
                                   ▼
                       Final Output + Audit Trace
```

### Actor Responsibilities & Least Privilege

| Actor | Input | Responsibility | Allowed Tools | Handoff Target |
| :--- | :--- | :--- | :--- | :--- |
| **Entity Resolver** | Case metadata, raw names, partial IDs | Disambiguate customer, order, and seller identities | Candidate lookup, customer search | Coordinator |
| **Coordinator** | Verified entities & complaint | Deconstructs case, assigns specialist tasks | Case registry, status tracking | Domain specialists |
| **Order Specialist** | `order_id` | Verifies items, unit prices, product status | `get_order_details`, `get_product` | Conflict Resolver |
| **Shipment Specialist** | `order_id`, tracking code | Checks carrier events, delivery dates, delays | `get_shipment_status`, `get_tracking` | Conflict Resolver |
| **Payment Specialist** | `order_id`, payment ID | Analyzes transactions, installments, refunds | `get_payment_details`, `get_refunds` | Conflict Resolver |
| **Conflict Resolver** | Specialist findings + customer claims | Identifies discrepancies, applies policy rules | `get_policy_rules` | Verifier |
| **Verifier** | Draft resolution + evidence list | Audits invariants, schema validity, evidence refs | None (pure verification) | Final Output |

---

## 2. Agent-to-Agent (A2A) Protocol Specification

A2A messages must always be wrapped in a deterministic envelope. Never exchange unvalidated free-form strings between agents.

### A2A Message Envelope

```python
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Literal
import uuid

@dataclass(frozen=True)
class A2AMessage:
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    case_id: str
    sender: str
    recipient: str
    intent: Literal["task_assign", "task_result", "clarification_request", "handoff", "abort"]
    payload: dict[str, Any]
    hop_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def next_hop(self, recipient: str, intent: str, payload: dict[str, Any]) -> "A2AMessage":
        if self.hop_count >= 10:
            raise RuntimeError(f"A2A cycle detected: max hop count (10) exceeded for case {self.case_id}")
        return A2AMessage(
            case_id=self.case_id,
            sender=self.recipient,
            recipient=recipient,
            intent=intent,
            payload=payload,
            hop_count=self.hop_count + 1,
        )
```

### Anti-Looping and Timeout Rules

1. **Hop Count Limit**: Set a hard limit (e.g., `hop_count <= 8`). If exceeded, route to fallback coordinator resolution.
2. **Correlation ID Tracking**: Maintain a `visited_actors` set in case context:
   ```python
   if actor in context.visited_actors and context.is_unproductive_cycle():
       raise DeadlockException(f"Actor {actor} revisited without new evidence")
   ```
3. **Idempotent Hand-offs**: Every task assignment includes exact prerequisites; specialists return structured outputs or explicit failure codes.

---

## 3. Evidence Lifecycle & Trace Emission

Every tool interaction and multi-agent event must be auditable.

### Rules of Evidence Integrity
- **No Evidence Invention**: Never manufacture fake `evidence_ref` IDs.
- **No Cross-Case Reuse**: Evidence obtained under `case_A` must never be cited in `case_B`.
- **Consumption Logging**: Whenever a tool's output contributes to a specialist's conclusion, emit a `tool_result_consumed` event.

### Observable Trace Schema (v1)

```python
# Trace event emission pattern
trace.emit(
    case_id=case["case_id"],
    event_type="task_assigned",
    actor="coordinator",
    target="shipment-agent",
    attributes={"reason": "Investigate reported non-delivery"},
)

# When consuming tool results:
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="shipment-agent",
    tool_name="get_shipment_status",
    evidence_refs=[evidence["evidence_ref"]],
    attributes={"delivery_status": "delivered_late"},
)

# When handing off:
trace.emit(
    case_id=case["case_id"],
    event_type="handoff",
    actor="shipment-agent",
    target="conflict-resolver",
    attributes={"claim_supported": False},
)
```

---

## 4. Entity Resolution Pipeline

In many real-world cases, users provide ambiguous information (e.g., misspelled names, vague order descriptions).

1. **Candidate Retrieval**:
   - Query MCP candidate search with fuzzy attributes (zip code, surname, approximate date).
2. **Scoring & Ranking**:
   - Compute composite score: `match_score = (name_sim * 0.4) + (geo_match * 0.3) + (date_proximity * 0.3)`.
3. **Confidence Thresholding**:
   - `match_score >= 0.85`: High confidence -> accept candidate.
   - `0.60 <= match_score < 0.85`: Ambiguous -> query second-tier evidence (customer phone last 4 digits, order item match).
   - `match_score < 0.60`: Reject candidate. Log rejection reason in final trace.

---

## 5. Verification Invariants Before Finalizing

Before returning the final resolution object, the **Verifier Agent** MUST check:

1. **Schema Conformance**: Matches target JSON schema strictly.
2. **Evidence Ownership**: All `evidence_refs` cited in claims exist in the case's MCP audit log.
3. **Financial Invariance**: Refund amount $\le$ order total; individual item totals sum to subtotal.
4. **Timeline Invariance**: Delivery date $\ge$ shipping date $\ge$ order purchase date.
5. **Confidence Calibration**: Express confidence between 0.0 and 1.0 reflecting actual evidence completeness.
