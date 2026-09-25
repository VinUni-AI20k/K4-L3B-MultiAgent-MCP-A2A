from __future__ import annotations

from typing import Any

from .analysis import _extract_order_rows, analyze_case
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def _call_tool(
    gateway: EvidenceGateway,
    tool_name: str,
    case_id: str,
    actor: str,
    trace: TraceWriter,
    consumed_refs: set[str],
    **arguments: str,
) -> dict[str, Any] | None:
    try:
        evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
    except Exception:  # any tool/transport failure must not kill the case
        return None
    ref = evidence.get("evidence_ref") if isinstance(evidence, dict) else None
    if ref:
        consumed_refs.add(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref],
        )
    return evidence


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    consumed_refs: set[str] = set()
    ev: dict[str, dict[str, Any] | None] = {}

    # 1. coordinator task_assigned -> entity-agent
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
    )

    # 2. entity-agent calls get_customer_history(hint) and get_order(claimed order id)
    hint = case.get("customer_unique_id_hint")
    if hint:
        ev["get_customer_history"] = await _call_tool(
            gateway,
            "get_customer_history",
            case_id,
            "entity-agent",
            trace,
            consumed_refs,
            customer_unique_id=hint,
        )
    else:
        ev["get_customer_history"] = None

    history_data = (
        ev["get_customer_history"].get("data") if ev.get("get_customer_history") else None
    )
    history_rows = _extract_order_rows(history_data)
    history_order_ids = {
        r["order_id"] for r in history_rows if isinstance(r, dict) and r.get("order_id")
    }

    claimed_order_id = (case.get("customer_request") or {}).get("claimed_order_id")
    # NEVER call tools on candidate-* ids or ids not in history
    if (
        claimed_order_id
        and not str(claimed_order_id).startswith("candidate-")
        and claimed_order_id in history_order_ids
    ):
        ev["get_order"] = await _call_tool(
            gateway,
            "get_order",
            case_id,
            "entity-agent",
            trace,
            consumed_refs,
            order_id=claimed_order_id,
        )
        entity_resolved = ev["get_order"] is not None
        target_order_id = claimed_order_id if entity_resolved else None
    else:
        ev["get_order"] = None
        entity_resolved = False
        target_order_id = None

    # 3. handoff entity-agent->coordinator
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="ENTITY_RESOLVED" if entity_resolved else "ENTITY_NOT_FOUND",
    )

    # 4. coordinator task_assigned to specialists
    if target_order_id:
        # order-agent
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-agent",
        )
        ev["get_order_items"] = await _call_tool(
            gateway,
            "get_order_items",
            case_id,
            "order-agent",
            trace,
            consumed_refs,
            order_id=target_order_id,
        )
        ev["get_product_context"] = await _call_tool(
            gateway,
            "get_product_context",
            case_id,
            "order-agent",
            trace,
            consumed_refs,
            order_id=target_order_id,
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="coordinator",
        )

        # shipment-agent
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
        )
        ev["get_shipment_summary"] = await _call_tool(
            gateway,
            "get_shipment_summary",
            case_id,
            "shipment-agent",
            trace,
            consumed_refs,
            order_id=target_order_id,
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="coordinator",
        )

        # payment-agent
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
        )
        ev["get_payment_timeline"] = await _call_tool(
            gateway,
            "get_payment_timeline",
            case_id,
            "payment-agent",
            trace,
            consumed_refs,
            order_id=target_order_id,
        )

        claims = (case.get("customer_request") or {}).get("claims") or []
        has_refund_claim = any(
            str(c.get("topic", "")).startswith("refund_") for c in claims if isinstance(c, dict)
        )

        order_status = ""
        if ev.get("get_order") and isinstance(ev["get_order"].get("data"), dict):
            order_status = str(ev["get_order"]["data"].get("order_status", "")).lower()
        if not order_status and history_rows:
            for r in history_rows:
                if r.get("order_id") == target_order_id and r.get("order_status"):
                    order_status = str(r["order_status"]).lower()
                    break
        is_order_canceled = order_status in ("canceled", "unavailable")

        has_payment_refund = False
        if ev.get("get_payment_timeline") and isinstance(
            ev["get_payment_timeline"].get("data"), dict
        ):
            pay_events = ev["get_payment_timeline"]["data"].get("events", [])
            if isinstance(pay_events, list):
                for e in pay_events:
                    if isinstance(e, dict):
                        etype = str(e.get("event_type", "")).lower()
                        if "refund" in etype or "chargeback" in etype:
                            has_payment_refund = True
                            break

        if has_refund_claim or is_order_canceled or has_payment_refund:
            ev["get_refund_timeline"] = await _call_tool(
                gateway,
                "get_refund_timeline",
                case_id,
                "payment-agent",
                trace,
                consumed_refs,
                order_id=target_order_id,
            )
        else:
            ev["get_refund_timeline"] = None

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment-agent",
            target="coordinator",
        )

    # policy-agent
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
    )
    ev["get_policy"] = await _call_tool(
        gateway,
        "get_policy",
        case_id,
        "policy-agent",
        trace,
        consumed_refs,
        policy_version=case["policy_version"],
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="coordinator",
    )

    # 8. analyze_case + policy_decided
    output = analyze_case(case, ev)
    primary_issue = output.get("assessment", {}).get("primary_issue")
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
    )

    # 9. verifier
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier",
    )

    is_valid = True
    if hasattr(trace, "contracts") and trace.contracts is not None:
        try:
            trace.contracts.validate_output(output, f"verifier:{case_id}")
        except Exception:
            is_valid = False

    output_refs = set(output.get("evidence_refs", []))
    for ca in output.get("claim_assessments", []):
        if isinstance(ca, dict):
            output_refs.update(ca.get("evidence_refs", []))

    if not output_refs.issubset(consumed_refs):
        is_valid = False

    status = output.get("assessment", {}).get("case_status")
    refund_brl = output.get("financial_resolution", {}).get("recommended_refund_brl", 0.0)
    refund_lines = output.get("financial_resolution", {}).get("refund_lines", [])
    actions = output.get("resolution_actions", [])

    if status == "no_action" and (refund_brl != 0.0 or len(refund_lines) > 0):
        is_valid = False
    if refund_brl > 0.0:
        lines_sum = round(sum(line.get("amount_brl", 0.0) for line in refund_lines), 2)
        if abs(lines_sum - refund_brl) > 0.01:
            is_valid = False
        if status == "no_action":
            is_valid = False
    if len(actions) != len(set(actions)):
        is_valid = False

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="PASS" if is_valid else "FAIL",
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="verifier",
        target="coordinator",
    )

    return output
