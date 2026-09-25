from __future__ import annotations
from decimal import Decimal
from typing import Any
from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector
from .business_checks import validate_business_output

async def run(task: Task, collector: EvidenceCollector) -> Result:
    case = task.payload["case"]; entity = task.payload["entity"]; findings = task.payload.get("findings", {})
    refs: list[str] = []
    policy_data: dict[str, Any] = {}
    if "get_policy" in collector.tools:
        try:
            response = await collector.call(task.recipient, "get_policy", **{k: case[k] for k in ("issue_type", "claim_type") if k in case})
            collector.consume(task.recipient, [response["evidence_ref"]]); refs.append(response["evidence_ref"])
            policy_data = response.get("data") if isinstance(response.get("data"), dict) else {}
        except Exception: pass
    order = findings.get("order", {}).get("payload", {}); ship = findings.get("shipment", {}).get("payload", {}); pay = findings.get("payment", {}).get("payload", {})
    payment = pay.get("payment_analysis", {"verdict":"insufficient_evidence","captured_total_brl":None,"refunded_total_brl":None,"refundable_total_brl":None})
    ship_analysis = ship.get("shipment_analysis", {"verdict":"insufficient_evidence","late_seller_ids":[],"timeline_complete":False})
    issue = "insufficient_evidence"; causes=[]; parties=[]; actions=[]; status="needs_investigation"; confidence=0.2
    if ship_analysis["verdict"] == "seller_delay": issue="late_delivery_seller"; causes=[{"cause_code":"SELLER_DELAY","rank":1}]; parties=[{"party_type":"seller","party_id":ship_analysis["late_seller_ids"][0]}]; status="action_required"; actions=["investigate seller delivery delay"]
    elif ship_analysis["verdict"] == "logistics_delay": issue="late_delivery_logistics"; causes=[{"cause_code":"LOGISTICS_DELAY","rank":1}]; parties=[{"party_type":"logistics_provider","party_id":None}]; status="action_required"; actions=["investigate logistics delay"]
    elif payment["verdict"] == "duplicate_capture": issue="duplicate_charge"; causes=[{"cause_code":"DUPLICATE_CAPTURE","rank":1}]; status="action_required"; actions=["review duplicate payment capture"]
    elif payment["verdict"] in {"refund_pending","refund_failed"}: issue=payment["verdict"]; status="action_required"; actions=["review refund status"]
    else: issue = "valid_split_payment" if payment["verdict"] == "reconciled" else "insufficient_evidence"; confidence=0.7 if payment["verdict"] == "reconciled" else confidence
    available = payment.get("refundable_total_brl"); refund = float(available or 0) if policy_data.get("refund_full") else 0.0
    output = {"assessment":{"primary_issue":issue,"secondary_issues":[],"case_status":status,"confidence":confidence},"affected_entities":{"order_ids":entity["entity_resolution"]["resolved_order_ids"],"item_ids":order.get("item_ids",[]),"seller_ids":list(dict.fromkeys(order.get("seller_ids",[])+ship.get("seller_ids",[]))),"payment_references":pay.get("payment_references",[]),"shipment_ids":ship.get("shipment_ids",[])},"shipment_analysis":ship_analysis,"payment_analysis":payment,"root_cause_analysis":{"ranked_causes":causes,"responsible_parties":parties},"evidence_refs":list(dict.fromkeys(refs)),"data_conflicts":ship.get("data_conflicts",[])[:5],"financial_resolution":{"currency":"BRL","recommended_refund_brl":refund,"refund_lines":[{"reason_code":"POLICY_REFUND","amount_brl":refund,"entity_id":entity["entity_resolution"]["resolved_order_ids"][0]}] if refund else []},"resolution_actions":actions}
    validate_business_output(output)
    return Result(task.case_id, task.recipient, task.message_id, output, tuple(output["evidence_refs"]))
