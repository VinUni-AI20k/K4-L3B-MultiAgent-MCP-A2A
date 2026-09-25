from __future__ import annotations

import json
import logging
from typing import Any

from .ollama_client import OllamaAgentClient
from .trace import TraceWriter

logger = logging.getLogger("student_agent.conflict_resolver")

CONFLICT_RESOLVER_SYSTEM_PROMPT = """You are the Conflict Resolver Agent for Brazilian E-Commerce dispute cases.
You reconcile customer claims against authoritative system logs and specialist findings using Source Precedence:
1. MCP System Audit Logs (highest)
2. Carrier Tracking Records
3. Merchant Statements
4. Customer Claims (subjective)

Your goals:
1. Record data conflicts where customer statements contradict system/carrier logs.
2. Determine the single primary_issue:
   - 'canceled_order_paid'
   - 'unavailable_order_paid'
   - 'late_delivery_seller'
   - 'late_delivery_logistics'
   - 'valid_split_payment'
   - 'payment_mismatch'
   - 'duplicate_charge'
   - 'refund_pending'
   - 'refund_failed'
   - 'unsupported_claim'
   - 'insufficient_evidence'
3. Determine case_status: 'action_required' | 'no_action' | 'needs_investigation'
4. Rank root causes (cause_code must match ^[A-Z][A-Z0-9_]{2,79}$) and identify responsible parties.
5. Produce financial resolution in BRL and recommended resolution actions.

Output strict JSON:
{
  "primary_issue": "<primary_issue>",
  "secondary_issues": ["<issue>", ...],
  "case_status": "action_required" | "no_action" | "needs_investigation",
  "data_conflicts": [
    {
      "field": "delivery_date",
      "sources": ["customer_claim", "mcp_carrier_log"],
      "selected_source": "mcp_carrier_log",
      "resolution_code": "SYSTEM_TIMESTAMP_PRECEDENCE"
    }
  ],
  "root_cause_analysis": {
    "ranked_causes": [
      {"cause_code": "SELLER_DISPATCH_TIMEOUT", "rank": 1}
    ],
    "responsible_parties": [
      {"party_type": "seller", "party_id": "<seller_id>"}
    ]
  },
  "financial_resolution": {
    "currency": "BRL",
    "recommended_refund_brl": 50.0,
    "refund_lines": [
      {"reason_code": "LATE_DISPATCH_COMPENSATION", "amount_brl": 50.0, "entity_id": "<seller_id>"}
    ]
  },
  "resolution_actions": [
    "issue_customer_refund",
    "penalize_seller_late_dispatch"
  ]
}
"""

VALID_PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}


class ConflictResolverAgent:
    """Specialist agent powered by Qwen3:1.7b resolving contradictions and synthesizing root causes."""

    def __init__(self, model: str = "qwen3:1.7b", host: str | None = None) -> None:
        self.actor_name = "conflict_resolver"
        self.client = OllamaAgentClient(
            model=model,
            system_prompt=CONFLICT_RESOLVER_SYSTEM_PROMPT,
            host=host,
            temperature=0.0,
        )

    async def resolve(
        self,
        case: dict[str, Any],
        entity_res: dict[str, Any],
        shipment_res: dict[str, Any],
        payment_res: dict[str, Any],
        policy_res: dict[str, Any],
        trace: TraceWriter,
    ) -> dict[str, Any]:
        from .cases import extract_case_details

        info = extract_case_details(case)
        case_id = info["case_id"]
        claims = info["claims"]
        resolved_orders = entity_res.get("resolved_order_ids", [])
        late_sellers = shipment_res.get("late_seller_ids", [])
        shipment_verdict = shipment_res.get("verdict", "insufficient_evidence")
        payment_verdict = payment_res.get("verdict", "insufficient_evidence")
        refundable_total = float(payment_res.get("refundable_total_brl", 0.0) or 0.0)

        # Baseline heuristic deduction
        primary_issue = "insufficient_evidence"
        case_status = "needs_investigation"
        ranked_causes: list[dict[str, Any]] = []
        responsible_parties: list[dict[str, Any]] = []
        recommended_refund_brl = 0.0
        refund_lines: list[dict[str, Any]] = []
        resolution_actions: list[str] = []
        data_conflicts: list[dict[str, Any]] = []

        # Evaluate based on specialist findings
        if shipment_verdict == "seller_delay":
            primary_issue = "late_delivery_seller"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "SELLER_DISPATCH_TIMEOUT", "rank": 1})
            party_id = late_sellers[0] if late_sellers else None
            responsible_parties.append({"party_type": "seller", "party_id": party_id})
            refund_amount = min(refundable_total, 25.0) if refundable_total > 0 else 0.0
            recommended_refund_brl = refund_amount
            if refund_amount > 0:
                refund_lines.append({
                    "reason_code": "SELLER_DELAY_COMPENSATION",
                    "amount_brl": refund_amount,
                    "entity_id": party_id,
                })
            resolution_actions = ["notify_seller_delay_penalty", "compensate_customer"]
        elif shipment_verdict == "logistics_delay":
            primary_issue = "late_delivery_logistics"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1})
            responsible_parties.append({"party_type": "logistics_provider", "party_id": "carrier_main"})
            refund_amount = min(refundable_total, 15.0) if refundable_total > 0 else 0.0
            recommended_refund_brl = refund_amount
            if refund_amount > 0:
                refund_lines.append({
                    "reason_code": "LOGISTICS_DELAY_COMPENSATION",
                    "amount_brl": refund_amount,
                    "entity_id": resolved_orders[0] if resolved_orders else None,
                })
            resolution_actions = ["open_logistics_inquiry", "issue_freight_compensation"]
        elif payment_verdict == "duplicate_capture":
            primary_issue = "duplicate_charge"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "PAYMENT_GATEWAY_DUPLICATE_CAPTURE", "rank": 1})
            responsible_parties.append({"party_type": "payment_provider", "party_id": "gateway_payment"})
            recommended_refund_brl = refundable_total / 2 if refundable_total > 0 else 50.0
            refund_lines.append({
                "reason_code": "DUPLICATE_CHARGE_REFUND",
                "amount_brl": recommended_refund_brl,
                "entity_id": resolved_orders[0] if resolved_orders else None,
            })
            resolution_actions = ["refund_duplicate_charge"]
        elif payment_verdict == "capture_mismatch":
            primary_issue = "payment_mismatch"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "PAYMENT_AMOUNT_MISMATCH", "rank": 1})
            responsible_parties.append({"party_type": "payment_provider", "party_id": "gateway_payment"})
            recommended_refund_brl = refundable_total
            refund_lines.append({
                "reason_code": "AMOUNT_MISMATCH_REFUND",
                "amount_brl": recommended_refund_brl,
                "entity_id": resolved_orders[0] if resolved_orders else None,
            })
            resolution_actions = ["correct_payment_mismatch"]
        elif payment_verdict == "refund_pending":
            primary_issue = "refund_pending"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "REFUND_GATEWAY_PROCESSING_DELAY", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "platform_ops"})
            recommended_refund_brl = refundable_total
            resolution_actions = ["expedite_pending_refund"]
        elif payment_verdict == "refund_failed":
            primary_issue = "refund_failed"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "REFUND_GATEWAY_REVERSAL_ERROR", "rank": 1})
            responsible_parties.append({"party_type": "payment_provider", "party_id": "gateway_payment"})
            recommended_refund_brl = refundable_total
            refund_lines.append({
                "reason_code": "FAILED_REFUND_RETRY",
                "amount_brl": recommended_refund_brl,
                "entity_id": resolved_orders[0] if resolved_orders else None,
            })
            resolution_actions = ["retry_failed_refund"]
        elif shipment_verdict == "lost":
            primary_issue = "canceled_order_paid"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "SHIPMENT_LOST_IN_TRANSIT", "rank": 1})
            responsible_parties.append({"party_type": "logistics_provider", "party_id": "carrier_main"})
            recommended_refund_brl = refundable_total
            refund_lines.append({
                "reason_code": "FULL_REFUND_LOST_ORDER",
                "amount_brl": recommended_refund_brl,
                "entity_id": resolved_orders[0] if resolved_orders else None,
            })
            resolution_actions = ["issue_full_refund", "file_carrier_lost_claim"]
        elif payment_res.get("is_split_payment"):
            primary_issue = "valid_split_payment"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "CUSTOMER_SPLIT_PAYMENT_MISUNDERSTANDING", "rank": 1})
            responsible_parties.append({"party_type": "customer", "party_id": None})
            recommended_refund_brl = 0.0
            refund_lines = []
            resolution_actions = ["send_split_payment_explanation"]
        elif shipment_verdict == "on_time" and payment_verdict in ["reconciled", "refunded"]:
            primary_issue = "unsupported_claim"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "CUSTOMER_UNSUBSTANTIATED_DISPUTE", "rank": 1})
            responsible_parties.append({"party_type": "customer", "party_id": None})
            recommended_refund_brl = 0.0
            refund_lines = []
            resolution_actions = ["close_case_unsupported"]

        # If still insufficient_evidence, infer from claims topics
        if primary_issue == "insufficient_evidence":
            topics = [str(c.get("topic") or c.get("text") or "").lower() for c in claims]
            if any("split_payment" in t for t in topics):
                primary_issue = "valid_split_payment"
                case_status = "no_action"
                ranked_causes.append({"cause_code": "CUSTOMER_SPLIT_PAYMENT_MISUNDERSTANDING", "rank": 1})
                responsible_parties.append({"party_type": "customer", "party_id": None})
                recommended_refund_brl = 0.0
                refund_lines = []
                resolution_actions = ["send_split_payment_explanation"]
            elif any("unsupported" in t for t in topics):
                primary_issue = "unsupported_claim"
                case_status = "no_action"
                ranked_causes.append({"cause_code": "CUSTOMER_UNSUBSTANTIATED_DISPUTE", "rank": 1})
                responsible_parties.append({"party_type": "customer", "party_id": None})
                recommended_refund_brl = 0.0
                refund_lines = []
                resolution_actions = ["close_case_unsupported"]
            elif any("seller" in t for t in topics):
                primary_issue = "late_delivery_seller"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "SELLER_DISPATCH_TIMEOUT", "rank": 1})
                responsible_parties.append({"party_type": "seller", "party_id": late_sellers[0] if late_sellers else None})
                recommended_refund_brl = 25.0
                refund_lines.append({"reason_code": "SELLER_DELAY_COMPENSATION", "amount_brl": 25.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["notify_seller_delay_penalty", "compensate_customer"]
            elif any("logistics" in t for t in topics):
                primary_issue = "late_delivery_logistics"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1})
                responsible_parties.append({"party_type": "logistics_provider", "party_id": "carrier_main"})
                recommended_refund_brl = 15.0
                refund_lines.append({"reason_code": "LOGISTICS_DELAY_COMPENSATION", "amount_brl": 15.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["open_logistics_inquiry", "issue_freight_compensation"]
            elif any("duplicate" in t for t in topics):
                primary_issue = "duplicate_charge"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "PAYMENT_GATEWAY_DUPLICATE_CAPTURE", "rank": 1})
                responsible_parties.append({"party_type": "payment_provider", "party_id": "gateway_payment"})
                recommended_refund_brl = 50.0
                refund_lines.append({"reason_code": "DUPLICATE_CHARGE_REFUND", "amount_brl": 50.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["refund_duplicate_charge"]
            elif any("mismatch" in t for t in topics):
                primary_issue = "payment_mismatch"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "PAYMENT_AMOUNT_MISMATCH", "rank": 1})
                responsible_parties.append({"party_type": "payment_provider", "party_id": "gateway_payment"})
                recommended_refund_brl = 30.0
                refund_lines.append({"reason_code": "AMOUNT_MISMATCH_REFUND", "amount_brl": 30.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["correct_payment_mismatch"]
            elif any("canceled" in t for t in topics):
                primary_issue = "canceled_order_paid"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1})
                responsible_parties.append({"party_type": "platform", "party_id": "platform_ops"})
                recommended_refund_brl = 50.0
                refund_lines.append({"reason_code": "CANCELED_ORDER_FULL_REFUND", "amount_brl": 50.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["issue_full_refund"]
            elif any("unavailable" in t for t in topics):
                primary_issue = "unavailable_order_paid"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "INVENTORY_UNAVAILABLE_STOCKOUT", "rank": 1})
                responsible_parties.append({"party_type": "seller", "party_id": late_sellers[0] if late_sellers else None})
                recommended_refund_brl = 50.0
                refund_lines.append({"reason_code": "UNAVAILABLE_ITEM_REFUND", "amount_brl": 50.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["issue_stockout_refund"]
            elif any("refund_pending" in t for t in topics):
                primary_issue = "refund_pending"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "REFUND_GATEWAY_PROCESSING_DELAY", "rank": 1})
                responsible_parties.append({"party_type": "platform", "party_id": "platform_ops"})
                recommended_refund_brl = 50.0
                refund_lines.append({"reason_code": "PENDING_REFUND_SETTLEMENT", "amount_brl": 50.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["expedite_pending_refund"]
            elif any("refund_failed" in t for t in topics):
                primary_issue = "refund_failed"
                case_status = "action_required"
                ranked_causes.append({"cause_code": "REFUND_GATEWAY_REVERSAL_ERROR", "rank": 1})
                responsible_parties.append({"party_type": "payment_provider", "party_id": "gateway_payment"})
                recommended_refund_brl = 50.0
                refund_lines.append({"reason_code": "FAILED_REFUND_RETRY", "amount_brl": 50.0, "entity_id": resolved_orders[0] if resolved_orders else None})
                resolution_actions = ["retry_failed_refund"]

        # Detect data conflict between claim and system record
        for c in claims:
            c_text = str(c.get("topic") or c.get("text") or "").lower()
            if "not delivered" in c_text or "never received" in c_text or "unsupported" in c_text:
                if shipment_verdict == "on_time" or primary_issue == "unsupported_claim":
                    data_conflicts.append({
                        "field": "delivery_status",
                        "sources": ["customer_claim", "carrier_tracking_log"],
                        "selected_source": "carrier_tracking_log",
                        "resolution_code": "CARRIER_DELIVERY_CONFIRMED",
                    })
            if ("charged twice" in c_text or "duplicate" in c_text or "split" in c_text) and primary_issue == "valid_split_payment":
                data_conflicts.append({
                    "field": "payment_charge_count",
                    "sources": ["customer_claim", "gateway_audit_log"],
                    "selected_source": "gateway_audit_log",
                    "resolution_code": "VALID_SPLIT_PAYMENT_VERIFIED",
                })

        # Ask Qwen 1.7b to synthesize and harmonize
        prompt = (
            f"Case ID: {case_id}\n"
            f"Claims: {json.dumps(claims, ensure_ascii=False)}\n"
            f"Shipment Findings: {json.dumps(shipment_res, ensure_ascii=False)}\n"
            f"Payment Findings: {json.dumps(payment_res, ensure_ascii=False)}\n"
            f"Policy Findings: {json.dumps(policy_res, ensure_ascii=False)}\n"
            f"Calculated Baseline Primary Issue: {primary_issue}\n"
            "Produce strict JSON resolution synthesis."
        )

        _, _, parsed = await self.client.chat(prompt)

        final_primary = primary_issue
        final_secondary: list[str] = []
        final_case_status = case_status

        if parsed:
            p_issue = parsed.get("primary_issue")
            if p_issue in VALID_PRIMARY_ISSUES:
                final_primary = p_issue
            if isinstance(parsed.get("secondary_issues"), list):
                final_secondary = [str(s) for s in parsed["secondary_issues"]][:5]
            if parsed.get("case_status") in ["action_required", "no_action", "needs_investigation"]:
                final_case_status = parsed["case_status"]
            if isinstance(parsed.get("data_conflicts"), list) and parsed["data_conflicts"]:
                data_conflicts = parsed["data_conflicts"][:5]

        # Enforce consistency guarantees
        if final_primary == "unsupported_claim":
            final_case_status = "no_action"
            recommended_refund_brl = 0.0
            refund_lines = []
        elif final_primary in ["canceled_order_paid", "unavailable_order_paid"]:
            final_case_status = "action_required"
        elif final_primary == "late_delivery_seller":
            final_case_status = "action_required"
            if not any(p.get("party_type") == "seller" for p in responsible_parties):
                responsible_parties.append({"party_type": "seller", "party_id": late_sellers[0] if late_sellers else None})
        elif final_primary == "late_delivery_logistics":
            final_case_status = "action_required"
            if not any(p.get("party_type") == "logistics_provider" for p in responsible_parties):
                responsible_parties.append({"party_type": "logistics_provider", "party_id": "carrier_main"})

        if final_case_status == "no_action":
            recommended_refund_brl = 0.0
            refund_lines = []

        # Deduplicate resolution_actions and limit to 8
        unique_actions = list(dict.fromkeys(resolution_actions))[:8]

        result = {
            "primary_issue": final_primary,
            "secondary_issues": final_secondary,
            "case_status": final_case_status,
            "data_conflicts": data_conflicts[:5],
            "root_cause_analysis": {
                "ranked_causes": ranked_causes[:5],
                "responsible_parties": responsible_parties[:5],
            },
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": round(recommended_refund_brl, 2),
                "refund_lines": refund_lines[:10],
            },
            "resolution_actions": unique_actions,
        }

        # Emit handoff to verifier
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor_name,
            target="verifier",
            attributes={
                "primary_issue": final_primary,
                "case_status": final_case_status,
                "data_conflicts": len(data_conflicts),
            },
        )

        return result
