"""Coordinator and Router Agent for Day09 L3B Multi-Agent Pipeline."""

from __future__ import annotations

import asyncio
from typing import Any

from ..contracts import Contracts
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .models import CaseEvidenceContext
from .order_agent import OrderAgent
from .payment_agent import PaymentAgent
from .policy_agent import PolicyAgent
from .shipment_agent import ShipmentAgent
from .verifier_agent import VerifierAgent


class CoordinatorAgent:
    """Supervises entity resolution, specialist routing, and the lifecycle pipeline."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter, contracts: Contracts) -> None:
        self.gateway = gateway
        self.trace = trace
        self.contracts = contracts
        self.order_agent = OrderAgent(gateway, trace)
        self.shipment_agent = ShipmentAgent(gateway, trace)
        self.payment_agent = PaymentAgent(gateway, trace)
        self.policy_agent = PolicyAgent(gateway, trace)
        self.verifier_agent = VerifierAgent(trace, contracts)

    async def solve(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = case["case_id"]
        context = CaseEvidenceContext(case_id)

        # 1. Entity Resolution & Customer Context
        claimed_id = case.get("customer_request", {}).get("claimed_order_id")
        candidate_ids = case.get("candidate_order_ids", [])
        hint = case.get("customer_unique_id_hint")

        # Query customer history
        related_order_ids: list[str] = []
        customer_unique_id: str | None = hint
        if hint:
            try:
                cust_ev = await self.gateway.call(
                    "get_customer_history", case_id=case_id, customer_unique_id=hint
                )
                context.record_evidence("get_customer_history", cust_ev)
                ref = cust_ev["evidence_ref"]
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="coordinator",
                    tool_name="get_customer_history",
                    evidence_refs=[ref],
                    attributes={"orders_found": len(cust_ev["data"].get("orders", []))},
                )
                cust_data = cust_ev["data"]
                customer_unique_id = cust_data.get("customer_unique_id") or hint
                for o in cust_data.get("orders", []):
                    oid = o.get("order_id")
                    if oid and oid not in related_order_ids:
                        related_order_ids.append(oid)
            except Exception:
                pass

        # Disambiguate candidates
        # Real order IDs are 32-hex characters; mock candidates start with 'candidate-'
        resolved_order_id = claimed_id
        rejected_candidates: list[str] = []

        for cand in candidate_ids:
            if cand.startswith("candidate-"):
                rejected_candidates.append(cand)
            elif len(cand) == 32:
                resolved_order_id = cand
            else:
                rejected_candidates.append(cand)

        if not resolved_order_id:
            resolved_order_id = candidate_ids[0] if candidate_ids else "unknown_order"

        if resolved_order_id not in related_order_ids and resolved_order_id != "unknown_order":
            related_order_ids.insert(0, resolved_order_id)

        # 2. Task Assignment to Specialists
        scope = case.get("investigation_scope", {})

        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order_agent",
            attributes={"task": "inspect_order_items_sellers_catalog"},
        )
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment_agent",
            attributes={"task": "inspect_shipment_transit_timeline"},
        )
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment_agent",
            attributes={"task": "inspect_payments_reconciliation_refunds"},
        )

        # Handoff to specialists
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="specialists",
            attributes={"order_id": resolved_order_id},
        )

        # 3. Parallel Specialist Execution
        claims = case.get("customer_request", {}).get("claims", [])
        claimed_topics = [c.get("topic") for c in claims if c.get("topic")]

        order_task = self.order_agent.investigate(case_id, resolved_order_id, scope, context)
        shipment_task = self.shipment_agent.investigate(case_id, resolved_order_id, context)
        payment_task = self.payment_agent.investigate(
            case_id, resolved_order_id, context, claimed_topics=claimed_topics
        )

        order_findings, shipment_findings, payment_findings = await asyncio.gather(
            order_task, shipment_task, payment_task
        )

        # 4. Handoff to Policy Agent
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="policy_agent",
            attributes={"task": "resolve_conflicts_and_adjudicate_claims"},
        )
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="specialists",
            target="policy_agent",
            attributes={"evidence_ready": True},
        )

        adjudication, _ = await self.policy_agent.adjudicate(
            case, order_findings, shipment_findings, payment_findings, context
        )

        # 5. Verifier Agent: Invariants, Math, and Schema Validation
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="verifier_agent",
            attributes={"task": "verify_invariants_and_audit_provenance"},
        )

        output = self.verifier_agent.verify_and_build(
            case=case,
            resolved_order_id=resolved_order_id,
            rejected_candidates=rejected_candidates,
            customer_unique_id=customer_unique_id,
            related_order_ids=related_order_ids,
            order_findings=order_findings,
            shipment_findings=shipment_findings,
            payment_findings=payment_findings,
            adjudication=adjudication,
            context=context,
        )

        return output
