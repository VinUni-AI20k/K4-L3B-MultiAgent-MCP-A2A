from __future__ import annotations

import json
import logging
from typing import Any

from .mcp_evidence import EvidenceManager
from .ollama_client import OllamaAgentClient
from .trace import TraceWriter

logger = logging.getLogger("student_agent.specialists")

# ============================================================================
# 1. SHIPMENT AGENT
# ============================================================================

SHIPMENT_SYSTEM_PROMPT = """You are the Shipment Specialist Agent for Brazilian E-Commerce dispute cases.
You analyze shipping timelines, carrier delivery logs, and seller dispatch limits.
Verdict rules:
- 'seller_delay': order_delivered_carrier_date > shipping_limit_date
- 'logistics_delay': delivered_carrier on time, but order_delivered_customer_date > order_estimated_delivery_date
- 'on_time': delivered to customer on or before order_estimated_delivery_date
- 'lost': shipment lost or never delivered past estimated delivery date
- 'returned': shipment returned to sender
- 'insufficient_evidence': missing timeline or no shipping records

Output valid JSON:
{
  "verdict": "on_time" | "seller_delay" | "logistics_delay" | "lost" | "returned" | "conflicting" | "insufficient_evidence",
  "late_seller_ids": ["<seller_id>", ...],
  "timeline_complete": true | false,
  "carrier_delivered_customer_date": "<iso_timestamp or null>",
  "estimated_delivery_date": "<iso_timestamp or null>",
  "shipment_ids": ["<id>", ...]
}
"""


class ShipmentAgent:
    """Specialist agent powered by Qwen3:1.7b analyzing logistics timelines."""

    def __init__(self, model: str = "qwen3:1.7b", host: str | None = None) -> None:
        self.actor_name = "shipment_agent"
        self.client = OllamaAgentClient(
            model=model,
            system_prompt=SHIPMENT_SYSTEM_PROMPT,
            host=host,
            temperature=0.0,
        )

    async def investigate(
        self,
        case_id: str,
        resolved_order_ids: list[str],
        evidence_mgr: EvidenceManager,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        # 1. Query MCP tools
        shipment_data: list[dict[str, Any]] = []
        late_sellers: list[str] = []
        shipment_ids: list[str] = []
        timeline_complete = False
        verdict = "insufficient_evidence"

        shipment_tool = evidence_mgr.find_matching_tool(
            "get_shipment_status", "get_shipment_details", "get_shipment_timeline", "get_order_details"
        )

        for oid in resolved_order_ids:
            if shipment_tool:
                res = await evidence_mgr.call_tool(shipment_tool, actor=self.actor_name, order_id=oid)
                if res and res.get("data"):
                    shipment_data.append(res["data"])

        # 2. Extract facts and compute deterministic baseline
        for data in shipment_data:
            sid = data.get("shipment_id") or data.get("tracking_number")
            if sid and sid not in shipment_ids:
                shipment_ids.append(str(sid))

            purchase_ts = data.get("order_purchase_timestamp")
            delivered_carrier = data.get("order_delivered_carrier_date")
            delivered_customer = data.get("order_delivered_customer_date")
            estimated_delivery = data.get("order_estimated_delivery_date")
            shipping_limit = data.get("shipping_limit_date")
            order_status = data.get("order_status", "")
            seller_id = data.get("seller_id")

            if purchase_ts and delivered_carrier and delivered_customer and estimated_delivery:
                timeline_complete = True

            if order_status in ["canceled", "unavailable"]:
                verdict = "lost"
            elif shipping_limit and delivered_carrier and delivered_carrier > shipping_limit:
                verdict = "seller_delay"
                if seller_id and seller_id not in late_sellers:
                    late_sellers.append(seller_id)
            elif estimated_delivery and delivered_customer and delivered_customer > estimated_delivery:
                verdict = "logistics_delay"
            elif estimated_delivery and delivered_customer and delivered_customer <= estimated_delivery:
                verdict = "on_time"
            elif delivered_customer is None and estimated_delivery:
                verdict = "lost"

        # 3. Consult Qwen 1.7b for refinement
        prompt = (
            f"Case ID: {case_id}\n"
            f"Resolved Orders: {json.dumps(resolved_order_ids)}\n"
            f"Shipment Data: {json.dumps(shipment_data, ensure_ascii=False)}\n"
            f"Calculated Verdict: {verdict}\n"
            f"Late Sellers: {json.dumps(late_sellers)}\n"
            "Produce strict JSON shipment analysis."
        )

        _, _, parsed = await self.client.chat(prompt)

        final_verdict = verdict
        final_late_sellers = late_sellers
        final_timeline_complete = timeline_complete

        if parsed:
            p_verdict = parsed.get("verdict")
            if p_verdict in ["on_time", "seller_delay", "logistics_delay", "lost", "returned", "conflicting", "insufficient_evidence"]:
                final_verdict = p_verdict
            if isinstance(parsed.get("late_seller_ids"), list):
                final_late_sellers = list(set(final_late_sellers + parsed["late_seller_ids"]))
            if isinstance(parsed.get("timeline_complete"), bool):
                final_timeline_complete = parsed["timeline_complete"]

        result = {
            "verdict": final_verdict,
            "late_seller_ids": final_late_sellers,
            "timeline_complete": final_timeline_complete,
            "shipment_ids": shipment_ids,
            "raw_data": shipment_data,
        }

        # Emit handoff event to conflict_resolver
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor_name,
            target="conflict_resolver",
            attributes={"verdict": final_verdict, "late_sellers": len(final_late_sellers)},
        )

        return result


# ============================================================================
# 2. PAYMENT AGENT
# ============================================================================

PAYMENT_SYSTEM_PROMPT = """You are the Payment Specialist Agent for Brazilian E-Commerce dispute cases.
You reconcile captured amounts, payment methods (credit card, boleto, voucher), order items sum, and refund logs.
Verdicts:
- 'reconciled': captured total equals items total with no pending refund
- 'capture_mismatch': captured amount differs from item total + freight
- 'duplicate_capture': customer was charged multiple times for the same transaction
- 'refund_pending': refund is approved/requested but not yet settled
- 'refund_failed': refund transaction failed
- 'refunded': full or proper refund already completed
- 'insufficient_evidence': missing payment transaction data

Output valid JSON:
{
  "verdict": "reconciled" | "capture_mismatch" | "duplicate_capture" | "refund_pending" | "refund_failed" | "refunded" | "insufficient_evidence",
  "captured_total_brl": 150.50,
  "refunded_total_brl": 0.0,
  "refundable_total_brl": 150.50,
  "payment_references": ["<ref>", ...],
  "is_split_payment": false,
  "recommended_refund_brl": 0.0,
  "refund_reason_code": "<REASON_CODE or null>"
}
"""


class PaymentAgent:
    """Specialist agent powered by Qwen3:1.7b reconciling payments and calculating refunds."""

    def __init__(self, model: str = "qwen3:1.7b", host: str | None = None) -> None:
        self.actor_name = "payment_agent"
        self.client = OllamaAgentClient(
            model=model,
            system_prompt=PAYMENT_SYSTEM_PROMPT,
            host=host,
            temperature=0.0,
        )

    async def investigate(
        self,
        case_id: str,
        resolved_order_ids: list[str],
        evidence_mgr: EvidenceManager,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        payment_data: list[dict[str, Any]] = []
        item_data: list[dict[str, Any]] = []
        refund_data: list[dict[str, Any]] = []
        payment_refs: list[str] = []

        payment_tool = evidence_mgr.find_matching_tool(
            "get_payment_details", "get_order_payments", "get_payments"
        )
        item_tool = evidence_mgr.find_matching_tool(
            "get_order_items", "get_items", "get_product_info"
        )
        refund_tool = evidence_mgr.find_matching_tool(
            "get_refund_status", "get_refund_details"
        )

        for oid in resolved_order_ids:
            if payment_tool:
                res = await evidence_mgr.call_tool(payment_tool, actor=self.actor_name, order_id=oid)
                if res and res.get("data"):
                    p_info = res["data"]
                    if isinstance(p_info, list):
                        payment_data.extend(p_info)
                    elif isinstance(p_info, dict):
                        payment_data.append(p_info)

            if item_tool:
                res = await evidence_mgr.call_tool(item_tool, actor=self.actor_name, order_id=oid)
                if res and res.get("data"):
                    i_info = res["data"]
                    if isinstance(i_info, list):
                        item_data.extend(i_info)
                    elif isinstance(i_info, dict):
                        item_data.append(i_info)

            if refund_tool:
                res = await evidence_mgr.call_tool(refund_tool, actor=self.actor_name, order_id=oid)
                if res and res.get("data"):
                    r_info = res["data"]
                    if isinstance(r_info, list):
                        refund_data.extend(r_info)
                    elif isinstance(r_info, dict):
                        refund_data.append(r_info)

        # Calculate totals in BRL
        captured_total = 0.0
        items_total = 0.0
        refunded_total = 0.0

        for p in payment_data:
            val = float(p.get("payment_value", 0.0) or 0.0)
            captured_total += val
            pref = p.get("payment_reference") or p.get("payment_id") or p.get("payment_sequential")
            if pref:
                payment_refs.append(str(pref))

        for it in item_data:
            price = float(it.get("price", 0.0) or 0.0)
            freight = float(it.get("freight_value", 0.0) or 0.0)
            items_total += (price + freight)

        for r in refund_data:
            r_val = float(r.get("refund_value", 0.0) or r.get("amount", 0.0) or 0.0)
            refunded_total += r_val

        captured_total = round(captured_total, 2)
        items_total = round(items_total, 2)
        refunded_total = round(refunded_total, 2)
        refundable_total = max(0.0, round(captured_total - refunded_total, 2))

        # Adjudicate verdict
        is_split = len(payment_data) > 1
        if captured_total == 0.0 and not payment_data:
            verdict = "insufficient_evidence"
        elif refund_data and any(r.get("status") == "pending" for r in refund_data):
            verdict = "refund_pending"
        elif refund_data and any(r.get("status") == "failed" for r in refund_data):
            verdict = "refund_failed"
        elif refundable_total == 0.0 and refunded_total > 0:
            verdict = "refunded"
        elif items_total > 0 and captured_total > (items_total * 1.5):
            verdict = "duplicate_capture"
        elif items_total > 0 and abs(captured_total - items_total) > 0.05:
            verdict = "capture_mismatch"
        else:
            verdict = "reconciled"

        prompt = (
            f"Case ID: {case_id}\n"
            f"Payments: {json.dumps(payment_data, ensure_ascii=False)}\n"
            f"Items: {json.dumps(item_data, ensure_ascii=False)}\n"
            f"Refunds: {json.dumps(refund_data, ensure_ascii=False)}\n"
            f"Captured BRL: {captured_total}, Items total: {items_total}, Refunded: {refunded_total}\n"
            f"Calculated Verdict: {verdict}\n"
            "Produce strict JSON payment analysis."
        )

        _, _, parsed = await self.client.chat(prompt)

        final_verdict = verdict
        final_captured = captured_total
        final_refunded = refunded_total
        final_refundable = refundable_total

        if parsed:
            p_verdict = parsed.get("verdict")
            if p_verdict in ["reconciled", "capture_mismatch", "duplicate_capture", "refund_pending", "refund_failed", "refunded", "insufficient_evidence"]:
                final_verdict = p_verdict
            if parsed.get("captured_total_brl") is not None:
                final_captured = float(parsed["captured_total_brl"])
            if parsed.get("refunded_total_brl") is not None:
                final_refunded = float(parsed["refunded_total_brl"])
            if parsed.get("refundable_total_brl") is not None:
                final_refundable = float(parsed["refundable_total_brl"])

        result = {
            "verdict": final_verdict,
            "captured_total_brl": final_captured,
            "refunded_total_brl": final_refunded,
            "refundable_total_brl": final_refundable,
            "payment_references": payment_refs,
            "is_split_payment": is_split,
            "raw_payments": payment_data,
            "raw_items": item_data,
        }

        # Emit handoff event to conflict_resolver
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor_name,
            target="conflict_resolver",
            attributes={"verdict": final_verdict, "captured_brl": final_captured},
        )

        return result


# ============================================================================
# 3. POLICY AGENT
# ============================================================================

POLICY_SYSTEM_PROMPT = """You are the Policy Specialist Agent for Brazilian E-Commerce dispute cases.
You reference Brazilian Consumer Law (CDC) and EC_POLICY_V2 to determine claim validity:
- 7-day remorse cancellation right (Right of Regret / CDC Art. 49): valid full refund if canceled within 7 days of delivery or before shipment.
- Seller delivery delay: seller is responsible if carrier handoff was late.
- Carrier transit delay: logistics provider responsible if delivery exceeded estimated delivery date.
- Duplicate charge / payment mismatch: immediate refund required.

Output valid JSON:
{
  "claim_assessments": [
    {
      "claim_id": "<claim_id>",
      "verdict": "supported" | "unsupported" | "partially_supported" | "insufficient_evidence",
      "confidence": 0.9,
      "policy_clause": "EC_POLICY_V2_CLAUSE_7"
    }
  ],
  "applicable_policy": "EC_POLICY_V2",
  "policy_decision_code": "POLICY_REFUND_APPROVED" | "POLICY_CLAIM_REJECTED"
}
"""


class PolicyAgent:
    """Specialist agent powered by Qwen3:1.7b consulting EC_POLICY_V2."""

    def __init__(self, model: str = "qwen3:1.7b", host: str | None = None) -> None:
        self.actor_name = "policy_agent"
        self.client = OllamaAgentClient(
            model=model,
            system_prompt=POLICY_SYSTEM_PROMPT,
            host=host,
            temperature=0.0,
        )

    async def investigate(
        self,
        case_id: str,
        claims: list[dict[str, Any]],
        evidence_mgr: EvidenceManager,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        policy_data: dict[str, Any] = {}
        policy_tool = evidence_mgr.find_matching_tool(
            "get_policy", "lookup_policy", "get_ecommerce_policy"
        )
        if policy_tool:
            res = await evidence_mgr.call_tool(policy_tool, actor=self.actor_name, policy_id="EC_POLICY_V2")
            if res and res.get("data"):
                policy_data = res["data"]

        prompt = (
            f"Case ID: {case_id}\n"
            f"Customer Claims: {json.dumps(claims, ensure_ascii=False)}\n"
            f"Policy EC_POLICY_V2: {json.dumps(policy_data, ensure_ascii=False)}\n"
            "Evaluate claims and return strict JSON assessment."
        )

        _, _, parsed = await self.client.chat(prompt)

        # Baseline claim assessment
        assessments: list[dict[str, Any]] = []
        for c in claims:
            cid = c.get("claim_id", f"claim_{len(assessments) + 1}")
            assessments.append({
                "claim_id": str(cid),
                "verdict": "supported",
                "confidence": 0.85,
                "evidence_refs": evidence_mgr.all_evidence_refs[:5],
            })

        if parsed and isinstance(parsed.get("claim_assessments"), list):
            llm_assessments = parsed["claim_assessments"]
            if len(llm_assessments) == len(claims):
                for idx, item in enumerate(llm_assessments):
                    v = item.get("verdict")
                    if v in ["supported", "unsupported", "partially_supported", "insufficient_evidence"]:
                        assessments[idx]["verdict"] = v
                    if item.get("confidence") is not None:
                        conf = float(item["confidence"])
                        assessments[idx]["confidence"] = max(0.0, min(1.0, conf))

        decision_code = parsed.get("policy_decision_code", "POLICY_EVALUATED") if parsed else "POLICY_EVALUATED"

        # Emit policy_decided and handoff events to trace
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.actor_name,
            decision_code=decision_code,
            attributes={"applicable_policy": "EC_POLICY_V2", "claim_count": len(claims)},
        )

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor_name,
            target="conflict_resolver",
            attributes={"assessed_claims": len(assessments)},
        )

        return {
            "claim_assessments": assessments,
            "policy_decision_code": decision_code,
        }
