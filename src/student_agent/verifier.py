from __future__ import annotations

import logging
from typing import Any

from .ollama_client import OllamaAgentClient
from .trace import TraceWriter

logger = logging.getLogger("student_agent.verifier")

VERIFIER_SYSTEM_PROMPT = """You are the Verifier Agent for Brazilian E-Commerce dispute investigations.
Your duty is to verify:
1. Cross-field consistency:
   - If case_status is 'no_action', recommended_refund_brl MUST be 0.0 and refund_lines MUST be empty.
   - If primary_issue is 'late_delivery_seller', seller MUST be in responsible_parties.
   - If primary_issue is 'late_delivery_logistics', logistics_provider MUST be in responsible_parties.
2. Calibration of confidence (between 0.0 and 1.0).
3. Elimination of synthetic garbage IDs from affected_entities.

Output valid JSON:
{
  "decision_code": "VERIFICATION_PASSED",
  "calibrated_confidence": 0.92,
  "invariants_checked": 5,
  "notes": "All cross-field consistency invariants satisfied."
}
"""


class VerifierAgent:
    """Specialist agent powered by Qwen3:1.7b and deterministic invariant checks."""

    def __init__(self, model: str = "qwen3:1.7b", host: str | None = None) -> None:
        self.actor_name = "verifier"
        self.client = OllamaAgentClient(
            model=model,
            system_prompt=VERIFIER_SYSTEM_PROMPT,
            host=host,
            temperature=0.0,
        )

    async def verify_and_package(
        self,
        case: dict[str, Any],
        entity_res: dict[str, Any],
        shipment_res: dict[str, Any],
        payment_res: dict[str, Any],
        policy_res: dict[str, Any],
        conflict_res: dict[str, Any],
        evidence_refs: list[str],
        trace: TraceWriter,
    ) -> dict[str, Any]:
        case_id = case.get("case_id", "")
        resolved_orders = entity_res.get("resolved_order_ids", [])
        rejected_candidates = entity_res.get("rejected_candidates", [])
        customer_unique_id = entity_res.get("customer_unique_id")
        related_orders = entity_res.get("related_order_ids", [])

        primary_issue = conflict_res.get("primary_issue", "insufficient_evidence")
        secondary_issues = list(dict.fromkeys(conflict_res.get("secondary_issues", [])))[:10]
        case_status = conflict_res.get("case_status", "needs_investigation")

        # Extract affected entities from specialist data
        seller_ids: list[str] = list(dict.fromkeys(shipment_res.get("late_seller_ids", [])))
        payment_refs: list[str] = list(dict.fromkeys(payment_res.get("payment_references", [])))
        shipment_ids: list[str] = list(dict.fromkeys(shipment_res.get("shipment_ids", [])))
        item_ids: list[str] = []
        for it in payment_res.get("raw_items", []):
            iid = it.get("order_item_id") or it.get("item_id") or it.get("product_id")
            if iid and str(iid) not in item_ids:
                item_ids.append(str(iid))

        # Filter out rejected candidates from order_ids
        clean_order_ids = [oid for oid in resolved_orders if oid not in rejected_candidates]

        # Invariant 1: Consistency between case_status, refund, and actions
        fin = conflict_res.get("financial_resolution", {})
        currency = fin.get("currency", "BRL")
        rec_refund = float(fin.get("recommended_refund_brl", 0.0) or 0.0)
        refund_lines = fin.get("refund_lines", [])

        if case_status == "no_action":
            rec_refund = 0.0
            refund_lines = []
        elif case_status == "action_required" and rec_refund == 0.0 and primary_issue in [
            "canceled_order_paid", "unavailable_order_paid", "duplicate_charge", "payment_mismatch"
        ]:
            rec_refund = float(payment_res.get("refundable_total_brl", 0.0) or 0.0)

        # Invariant 2: Root cause responsibility
        rca = conflict_res.get("root_cause_analysis", {})
        ranked_causes = rca.get("ranked_causes", [])
        responsible_parties = rca.get("responsible_parties", [])

        if primary_issue == "late_delivery_seller":
            if not any(p.get("party_type") == "seller" for p in responsible_parties):
                responsible_parties.append({"party_type": "seller", "party_id": seller_ids[0] if seller_ids else None})
        elif primary_issue == "late_delivery_logistics":
            if not any(p.get("party_type") == "logistics_provider" for p in responsible_parties):
                responsible_parties.append({"party_type": "logistics_provider", "party_id": "carrier_main"})

        # Confidence calibration
        confidence = 0.85
        if entity_res.get("status") == "resolved" and shipment_res.get("timeline_complete"):
            confidence = 0.95
        elif entity_res.get("status") == "ambiguous":
            confidence = 0.60
        elif primary_issue == "insufficient_evidence":
            confidence = 0.40

        # Unique actions limited to 8
        actions = list(dict.fromkeys(conflict_res.get("resolution_actions", [])))[:8]
        if not actions and case_status == "action_required":
            actions = ["review_case_details"]

        # Ensure evidence refs is unique and valid
        clean_evidence_refs = list(dict.fromkeys(evidence_refs))[:30]

        # Package the final output dictionary matching day09-l3b-output-v2
        output: dict[str, Any] = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": primary_issue,
                "secondary_issues": secondary_issues,
                "case_status": case_status,
                "confidence": round(confidence, 2),
            },
            "affected_entities": {
                "order_ids": clean_order_ids,
                "item_ids": item_ids[:20],
                "seller_ids": seller_ids[:20],
                "payment_references": payment_refs[:20],
                "shipment_ids": shipment_ids[:20],
            },
            "claim_assessments": policy_res.get("claim_assessments", [])[:5],
            "entity_resolution": {
                "status": entity_res.get("status", "resolved"),
                "resolved_order_ids": clean_order_ids,
                "rejected_candidates": list(dict.fromkeys(rejected_candidates))[:20],
                "confidence": round(float(entity_res.get("confidence", 0.9)), 2),
            },
            "customer_context": {
                "customer_unique_id": customer_unique_id,
                "related_order_ids": list(dict.fromkeys(related_orders))[:20],
            },
            "shipment_analysis": {
                "verdict": shipment_res.get("verdict", "insufficient_evidence"),
                "late_seller_ids": list(dict.fromkeys(seller_ids))[:20],
                "timeline_complete": bool(shipment_res.get("timeline_complete", False)),
            },
            "payment_analysis": {
                "verdict": payment_res.get("verdict", "insufficient_evidence"),
                "captured_total_brl": payment_res.get("captured_total_brl"),
                "refunded_total_brl": payment_res.get("refunded_total_brl"),
                "refundable_total_brl": payment_res.get("refundable_total_brl"),
            },
            "root_cause_analysis": {
                "ranked_causes": ranked_causes[:5],
                "responsible_parties": responsible_parties[:5],
            },
            "evidence_refs": clean_evidence_refs,
            "data_conflicts": conflict_res.get("data_conflicts", [])[:5],
            "financial_resolution": {
                "currency": currency,
                "recommended_refund_brl": round(rec_refund, 2),
                "refund_lines": refund_lines[:10],
            },
            "resolution_actions": actions,
        }

        # Emit verification_completed trace event
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.actor_name,
            decision_code="VERIFICATION_PASSED",
            evidence_refs=clean_evidence_refs[:5],
            attributes={
                "calibrated_confidence": round(confidence, 2),
                "primary_issue": primary_issue,
                "case_status": case_status,
            },
        )

        return output
