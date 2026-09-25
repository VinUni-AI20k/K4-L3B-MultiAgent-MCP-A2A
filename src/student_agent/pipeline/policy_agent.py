"""Policy and Conflict Resolution Specialist Agent powered by Gemini Flash Lite."""

from __future__ import annotations

import json
import os
from typing import Any

from google import genai
from google.genai import types

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .models import (
    AdjudicationDraft,
    CaseEvidenceContext,
    OrderFindings,
    PaymentFindings,
    ShipmentFindings,
)
from .prompts import POLICY_AGENT_SYSTEM_PROMPT

CAUSE_CODE_MAP = {
    "canceled_order_paid": "CANCELED_ORDER_PAID",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_PAID",
    "late_delivery_seller": "LATE_DELIVERY_SELLER",
    "late_delivery_logistics": "LATE_DELIVERY_LOGISTICS",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "refund_failed": "REFUND_GATEWAY_FAILURE",
    "refund_pending": "REFUND_PROCESSING_PENDING",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "unsupported_claim": "UNSUPPORTED_CUSTOMER_CLAIM",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}


class PolicyAgent:
    """Specialist responsible for policy, conflict reconciliation, and adjudication."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace
        api_key = os.getenv("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else None
        self.model_name = "gemini-3.5-flash-lite"
        self.fallback_model = "gemini-flash-lite-latest"

    async def adjudicate(
        self,
        case: dict[str, Any],
        order_findings: OrderFindings,
        shipment_findings: ShipmentFindings,
        payment_findings: PaymentFindings,
        context: CaseEvidenceContext,
    ) -> tuple[AdjudicationDraft, list[str]]:
        case_id = case["case_id"]
        policy_version = case.get("policy_version", "EC_POLICY_V2")
        evidence_refs: list[str] = []

        # 1. Fetch policy from MCP
        cached_policy = context.get_cached("get_policy", {"policy_version": policy_version})
        if cached_policy is None:
            policy_ev = await self.gateway.call(
                "get_policy", case_id=case_id, policy_version=policy_version
            )
            context.set_cached("get_policy", {"policy_version": policy_version}, policy_ev)
        else:
            policy_ev = cached_policy

        ref = policy_ev["evidence_ref"]
        evidence_refs.append(ref)
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="policy_agent",
            tool_name="get_policy",
            evidence_refs=[ref],
            attributes={"policy_version": policy_version},
        )
        policy_rules = policy_ev["data"].get("rules", {})

        # Prepare customer claims
        customer_request = case.get("customer_request", {})
        claims = customer_request.get("claims", [])
        claimed_topics = [c.get("topic") for c in claims]
        primary_claimed_topic = claimed_topics[0] if claimed_topics else "unsupported_claim"

        # 2. Invoke Gemini Flash Lite for reasoning and conflict resolution
        gemini_draft: AdjudicationDraft | None = None
        if self.client:
            prompt_payload = {
                "case_id": case_id,
                "complaint_message": customer_request.get("message", ""),
                "claims": claims,
                "order_findings": order_findings.model_dump(),
                "shipment_findings": shipment_findings.model_dump(),
                "payment_findings": payment_findings.model_dump(),
                "policy_rules": policy_rules,
            }
            user_prompt = (
                "Analyze the following dispute telemetry against policy rules and return "
                "the adjudication draft:\n" + json.dumps(prompt_payload, indent=2)
            )
            config = types.GenerateContentConfig(
                system_instruction=POLICY_AGENT_SYSTEM_PROMPT,
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=AdjudicationDraft,
            )
            for model_to_try in [self.model_name, self.fallback_model]:
                try:
                    chat = self.client.aio.chats.create(
                        model=model_to_try,
                        config=config,
                    )
                    response = await chat.send_message(user_prompt)
                    gemini_draft = AdjudicationDraft.model_validate_json(response.text)
                    break
                except Exception:
                    continue

        # 3. Authoritative Policy Rule Alignment
        primary_issue = primary_claimed_topic
        if primary_issue not in policy_rules:
            if gemini_draft and gemini_draft.primary_issue in policy_rules:
                primary_issue = gemini_draft.primary_issue
            else:
                primary_issue = "unsupported_claim"

        rule = policy_rules.get(primary_issue, {})
        case_status = rule.get("case_status", "action_required")
        if primary_issue in ("unsupported_claim", "valid_split_payment"):
            case_status = "no_action"
        recommended_action = rule.get("recommended_action", "document_no_action")
        if case_status == "no_action":
            recommended_action = "document_no_action"
            refund_brl = 0.0
        else:
            refund_brl = float(rule.get("refund_brl", 0.0))

            # Ground with actual specialist telemetry when available
            if primary_issue == "late_delivery_seller":
                target_seller = (
                    shipment_findings.late_seller_ids[0]
                    if shipment_findings.late_seller_ids
                    else (order_findings.seller_ids[0] if order_findings.seller_ids else None)
                )
                if target_seller and target_seller in order_findings.seller_financials:
                    seller_freight = order_findings.seller_financials[target_seller].get(
                        "freight", 0.0
                    )
                    if seller_freight > 0:
                        refund_brl = seller_freight
            elif primary_issue == "late_delivery_logistics":
                if order_findings.total_freight_brl > 0:
                    refund_brl = order_findings.total_freight_brl
            elif primary_issue == "duplicate_charge":
                if payment_findings.duplicate_amount_brl > 0:
                    refund_brl = payment_findings.duplicate_amount_brl
            elif primary_issue == "payment_mismatch":
                if payment_findings.mismatch_amount_brl > 0:
                    refund_brl = payment_findings.mismatch_amount_brl
            elif primary_issue == "refund_failed":
                if payment_findings.failed_refund_amount_brl > 0:
                    refund_brl = payment_findings.failed_refund_amount_brl

            # Financial consistency check: refund cannot exceed captured amount
            max_refundable = (
                payment_findings.refundable_total_brl
                if payment_findings.refundable_total_brl > 0
                else payment_findings.captured_total_brl
            )
            refund_brl = min(refund_brl, max_refundable) if max_refundable > 0 else refund_brl

        # Responsible parties
        raw_parties = rule.get("responsible_parties", [])
        responsible_parties = []
        for rp in raw_parties:
            ptype = rp.get("party_type")
            pid = rp.get("party_id")
            if ptype == "seller":
                # Prefer actual seller from this order if known
                if shipment_findings.late_seller_ids:
                    pid = shipment_findings.late_seller_ids[0]
                elif order_findings.seller_ids:
                    pid = order_findings.seller_ids[0]
            responsible_parties.append({"party_type": ptype, "party_id": pid})

        cause_code = CAUSE_CODE_MAP.get(primary_issue, "UNSUPPORTED_CUSTOMER_CLAIM")

        # 4. Claim Assessments
        claim_verdicts: dict[str, str] = {}
        partial_topics = (
            "late_delivery_seller",
            "late_delivery_logistics",
            "duplicate_charge",
            "payment_mismatch",
            "refund_failed",
            "refund_pending",
        )
        for c in claims:
            cid = c.get("claim_id")
            topic = c.get("topic")
            if topic == primary_issue:
                if primary_issue in ("unsupported_claim", "valid_split_payment"):
                    claim_verdicts[cid] = "unsupported"
                elif primary_issue == "refund_pending":
                    claim_verdicts[cid] = "partially_supported"
                else:
                    claim_verdicts[cid] = "supported"
            elif topic == "requested_full_refund":
                if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
                    claim_verdicts[cid] = "supported"
                elif primary_issue in partial_topics:
                    claim_verdicts[cid] = "partially_supported"
                else:
                    claim_verdicts[cid] = "unsupported"
            else:
                claim_verdicts[cid] = "unsupported"

        # Secondary issues
        has_full_refund_claim = "requested_full_refund" in claimed_topics
        secondary_issues = (
            ["requested_full_refund"]
            if has_full_refund_claim and primary_issue != "requested_full_refund"
            else []
        )

        # Confidence calibration
        if primary_issue in ("unsupported_claim", "refund_pending"):
            confidence = 0.85
        elif not shipment_findings.timeline_complete:
            confidence = 0.88
        else:
            confidence = 0.96

        fallback_rationale = f"Resolved based on {policy_version} rule {primary_issue}."
        final_adjudication = AdjudicationDraft(
            primary_issue=primary_issue,
            secondary_issues=secondary_issues,
            case_status=case_status,
            confidence=round(confidence, 2),
            recommended_action=recommended_action,
            recommended_refund_brl=round(refund_brl, 2),
            cause_code=cause_code,
            responsible_parties=responsible_parties,
            claim_verdicts=claim_verdicts,
            rationale=gemini_draft.rationale if gemini_draft else fallback_rationale,
        )

        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy_agent",
            decision_code=primary_issue,
            evidence_refs=[ref],
            attributes={
                "case_status": case_status,
                "recommended_action": recommended_action,
                "refund_brl": round(refund_brl, 2),
            },
        )

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="policy_agent",
            target="verifier_agent",
            attributes={"primary_issue": primary_issue},
        )

        return final_adjudication, evidence_refs
