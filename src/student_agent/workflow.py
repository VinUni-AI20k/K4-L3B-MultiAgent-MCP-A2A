from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

TOOL_ACTORS = {
    "get_order": "entity-resolver",
    "get_customer_history": "customer-agent",
    "get_order_items": "order-product-agent",
    "get_product_context": "order-product-agent",
    "get_shipment_summary": "shipment-agent",
    "get_payment_timeline": "payment-refund-agent",
    "get_refund_timeline": "payment-refund-agent",
    "get_policy": "policy-agent",
}


class _CaseContext:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self.evidence_refs: list[str] = []
        self.refs_by_tool: dict[str, list[str]] = {}
        self.failures: dict[str, str] = {}

    async def call(
        self, gateway: EvidenceGateway, trace: TraceWriter, name: str, **args: str
    ) -> dict[str, Any] | None:
        available_tools = getattr(gateway, "available_tools", None)
        if available_tools is not None and name not in available_tools:
            self.failures[name] = "tool_not_discovered"
            return None
        key = (name, tuple(sorted(args.items())))
        if key in self.cache:
            return self.cache[key]
        started = time.perf_counter()
        try:
            evidence = await asyncio.wait_for(
                gateway.call(name, case_id=self.case_id, **args), timeout=10
            )
        except (TimeoutError, RuntimeError, ValueError) as exc:
            self.failures[name] = type(exc).__name__
            return None
        ref = evidence["evidence_ref"]
        self.cache[key] = evidence
        if ref not in self.evidence_refs:
            self.evidence_refs.append(ref)
        self.refs_by_tool.setdefault(name, []).append(ref)
        trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=TOOL_ACTORS[name],
            tool_name=name,
            evidence_refs=[ref],
            attributes={"latency_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
        return evidence

    def handoff(self, trace: TraceWriter, sender: str, recipient: str, code: str) -> None:
        trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=sender,
            target=recipient,
            decision_code=code,
        )


def _data(value: dict[str, Any] | None, default: Any = None) -> Any:
    return value.get("data", default) if value else default


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ids(values: list[Any], field: str | None = None) -> list[str]:
    result = [v.get(field) if field and isinstance(v, dict) else v for v in values]
    return list(dict.fromkeys(str(v) for v in result if v))


def _not_after(left: Any, right: Any) -> bool:
    try:
        return datetime.fromisoformat(str(left)) <= datetime.fromisoformat(str(right))
    except (TypeError, ValueError):
        return False


def _finish(
    context: _CaseContext,
    trace: TraceWriter,
    case_id: str,
    output: dict[str, Any],
) -> None:
    missing_claims = sum(
        not assessment.get("evidence_refs") for assessment in output.get("claim_assessments", [])
    )
    context.handoff(trace, "conflict-resolver", "verifier", "verify_contract_and_evidence")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="output_contract_ready",
        evidence_refs=context.evidence_refs,
        attributes={
            "verified": not context.failures and missing_claims == 0,
            "total_tools": len(context.refs_by_tool) + len(context.failures),
            "total_evidence": len(context.evidence_refs),
            "missing_evidence": len(context.failures) + missing_claims,
        },
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    context = _CaseContext(case_id)
    request = case.get("customer_request", {})
    claims = request.get("claims", [])
    topics = [claim.get("topic", "") for claim in claims]
    primary_topic = next((topic for topic in topics if topic != "requested_full_refund"), "")
    scope = case.get("investigation_scope", {})
    candidates = [str(v) for v in case.get("candidate_order_ids", [])]
    claimed = str(request.get("claimed_order_id", ""))

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-resolver",
        decision_code="resolve_candidates",
    )
    context.handoff(trace, "coordinator", "entity-resolver", "resolve_candidates")
    orders: dict[str, dict[str, Any]] = {}
    for candidate in [claimed] if claimed else candidates:
        result = await context.call(gateway, trace, "get_order", order_id=candidate)
        data = _data(result, {})
        if isinstance(data, dict) and data.get("order_id") == candidate:
            orders[candidate] = result  # type: ignore[assignment]
    resolved = list(orders)
    rejected = [candidate for candidate in candidates if candidate not in resolved]
    resolution_status = (
        "resolved" if len(resolved) == 1 else "ambiguous" if resolved else "not_found"
    )
    context.handoff(trace, "entity-resolver", "coordinator", "entity_resolved")
    if len(resolved) != 1:
        output = _build_output(
            case, context, resolution_status, resolved, rejected, {}, {}, [], [], {}, {}, None, {}
        )
        _finish(context, trace, case_id, output)
        return output

    order_id = resolved[0]
    order = _data(orders[order_id], {})
    customer = None
    if scope.get("include_customer_history", True):
        context.handoff(trace, "coordinator", "customer-agent", "collect_customer_context")
        customer = await context.call(
            gateway,
            trace,
            "get_customer_history",
            customer_unique_id=str(case.get("customer_unique_id_hint", "")),
        )
        context.handoff(trace, "customer-agent", "coordinator", "customer_context_ready")
    products = None
    if scope.get("include_product_context", True):
        context.handoff(trace, "coordinator", "order-product-agent", "collect_order_product")
        products = await context.call(gateway, trace, "get_product_context", order_id=order_id)
    items = None
    if primary_topic in {"late_delivery_seller", "payment_mismatch"}:
        items = await context.call(gateway, trace, "get_order_items", order_id=order_id)
    if scope.get("include_product_context", True):
        context.handoff(trace, "order-product-agent", "coordinator", "order_product_ready")

    shipment = payment = refund = None
    if primary_topic in {"late_delivery_seller", "late_delivery_logistics"}:
        context.handoff(trace, "coordinator", "shipment-agent", "analyze_shipment")
        shipment = await context.call(gateway, trace, "get_shipment_summary", order_id=order_id)
        context.handoff(trace, "shipment-agent", "coordinator", "shipment_ready")
    elif primary_topic in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "payment_mismatch",
        "duplicate_charge",
        "valid_split_payment",
    }:
        context.handoff(trace, "coordinator", "payment-refund-agent", "analyze_payment")
        payment = await context.call(gateway, trace, "get_payment_timeline", order_id=order_id)
        context.handoff(trace, "payment-refund-agent", "coordinator", "payment_ready")
    elif primary_topic in {"refund_pending", "refund_failed"} or "requested_full_refund" in topics:
        context.handoff(trace, "coordinator", "payment-refund-agent", "analyze_refund")
        refund = await context.call(gateway, trace, "get_refund_timeline", order_id=order_id)
        context.handoff(trace, "payment-refund-agent", "coordinator", "refund_ready")

    context.handoff(trace, "coordinator", "policy-agent", "load_policy")
    policy = await context.call(
        gateway, trace, "get_policy", policy_version=str(case.get("policy_version", ""))
    )
    if policy:
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            decision_code="policy_loaded",
            evidence_refs=[policy["evidence_ref"]],
        )
    context.handoff(trace, "policy-agent", "conflict-resolver", "evidence_ready")
    output = _build_output(
        case,
        context,
        resolution_status,
        resolved,
        rejected,
        order,
        _data(customer, {}),
        _data(items, []),
        _data(products, []),
        _data(shipment, {}),
        _data(payment, {}),
        _data(refund),
        _data(policy, {}),
    )
    _finish(context, trace, case_id, output)
    return output


def _build_output(
    case: dict[str, Any],
    context: _CaseContext,
    resolution_status: str,
    resolved: list[str],
    rejected: list[str],
    order: dict[str, Any],
    customer: dict[str, Any],
    items: list[dict[str, Any]],
    products: list[dict[str, Any]],
    shipment: dict[str, Any],
    payment: dict[str, Any],
    refund: dict[str, Any] | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    claims = case.get("customer_request", {}).get("claims", [])
    topics = [claim.get("topic", "") for claim in claims]
    requested = next((topic for topic in topics if topic != "requested_full_refund"), "")
    events = shipment.get("events", [])
    late = bool(
        shipment.get("delivered_customer_at")
        and shipment.get("estimated_delivery_at")
        and not _not_after(shipment["delivered_customer_at"], shipment["estimated_delivery_at"])
    )
    old_event = any(
        event.get("event_type") == "delivered_late"
        and order.get("order_purchase_timestamp")
        and not _not_after(order["order_purchase_timestamp"], event.get("event_at"))
        for event in events
    )
    has_late_event = any(event.get("event_type") == "delivered_late" for event in events)
    shipment_verdict = (
        "seller_delay"
        if requested == "late_delivery_seller" and has_late_event
        else "logistics_delay"
        if late or (has_late_event and not old_event)
        else "conflicting"
        if old_event
        else "on_time"
        if shipment.get("delivered_customer_at") and shipment.get("estimated_delivery_at")
        else "insufficient_evidence"
    )
    payments = payment.get("payments", [])
    captured = sum(
        _number(e.get("amount_brl"))
        for e in payment.get("events", [])
        if e.get("event_type") == "captured"
    ) or sum(_number(row.get("payment_value")) for row in payments)
    declared = sum(_number(row.get("payment_value")) for row in payments)
    item_total = sum(_number(row.get("price")) + _number(row.get("freight_value")) for row in items)
    signatures = [
        (row.get("payment_type"), row.get("payment_value"), row.get("payment_installments"))
        for row in payments
    ]
    duplicate = bool(signatures) and len(signatures) != len(set(signatures))
    refund_events = (refund or {}).get("events", [])
    refund_statuses = {str(e.get("status", e.get("event_type", ""))).lower() for e in refund_events}
    refunded = sum(
        _number(e.get("amount_brl"))
        for e in refund_events
        if e.get("event_type") in {"refunded", "refund_completed"}
    )
    supported = {
        "canceled_order_paid": bool(order) and bool(payment),
        "unavailable_order_paid": bool(order) and bool(payment),
        "late_delivery_seller": bool(shipment) and has_late_event,
        "late_delivery_logistics": bool(shipment) and (late or has_late_event),
        "valid_split_payment": bool(payment) and len(payments) > 1 and not duplicate,
        "payment_mismatch": bool(payment)
        and (
            abs(captured - declared) >= 0.01
            or bool(item_total and abs(captured - item_total) >= 0.01)
        ),
        "duplicate_charge": bool(payment) and duplicate,
        "refund_pending": bool(refund_events) and "pending" in refund_statuses,
        "refund_failed": bool(refund_events)
        and bool(refund_statuses & {"failed", "failure", "refund_failed"}),
        "unsupported_claim": bool(order or shipment or payment),
    }
    primary = requested if supported.get(requested, False) else "insufficient_evidence"
    rule = policy.get("rules", {}).get(primary, {})
    refund_amount = _number(rule.get("refund_brl")) if primary != "insufficient_evidence" else 0.0
    customer_orders = customer.get("orders", [])
    conflicts = []
    timestamps = {
        row.get("order_purchase_timestamp")
        for row in customer_orders
        if row.get("order_id") in resolved
    }
    if len(timestamps) > 1:
        conflicts.append(
            {
                "field": "order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_order",
                "resolution_code": "authoritative_order_precedence",
            }
        )
    if old_event:
        conflicts.append(
            {
                "field": "shipment.event_at",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_order",
                "resolution_code": "event_before_authoritative_purchase",
            }
        )

    groups = {
        "canceled_order_paid": ["get_order", "get_payment_timeline", "get_policy"],
        "unavailable_order_paid": ["get_order", "get_payment_timeline", "get_policy"],
        "late_delivery_seller": [
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_policy",
        ],
        "late_delivery_logistics": ["get_order", "get_shipment_summary", "get_policy"],
        "payment_mismatch": ["get_order", "get_order_items", "get_payment_timeline", "get_policy"],
        "duplicate_charge": ["get_order", "get_payment_timeline", "get_policy"],
        "valid_split_payment": ["get_order", "get_payment_timeline", "get_policy"],
        "refund_pending": ["get_order", "get_refund_timeline", "get_policy"],
        "refund_failed": ["get_order", "get_refund_timeline", "get_policy"],
        "requested_full_refund": ["get_order", "get_refund_timeline", "get_policy"],
    }

    def refs(topic: str) -> list[str]:
        return list(
            dict.fromkeys(
                ref
                for tool in groups.get(topic, ["get_order"])
                for ref in context.refs_by_tool.get(tool, [])
            )
        )

    assessments = []
    for claim in claims:
        topic = claim.get("topic", "")
        verdict = (
            "supported"
            if topic == primary or (topic == "requested_full_refund" and refund_amount > 0)
            else "insufficient_evidence"
            if context.failures or conflicts
            else "unsupported"
        )
        assessments.append(
            {
                "claim_id": claim.get("claim_id", "unknown"),
                "verdict": verdict,
                "confidence": 0.88 if verdict == "supported" else 0.45,
                "evidence_refs": refs(topic),
            }
        )
    status = rule.get("case_status") if primary != "insufficient_evidence" else None
    status = status or ("needs_investigation" if context.failures or conflicts else "no_action")
    confidence = (
        0.88
        if primary != "insufficient_evidence" and not context.failures and not conflicts
        else 0.62
        if primary != "insufficient_evidence"
        else 0.45
    )
    sellers = _ids(items + products, "seller_id")
    item_ids = _ids(items + products, "order_item_id")
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": ["data_conflict"] if conflicts else [],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": _ids(resolved),
            "item_ids": item_ids,
            "seller_ids": sellers,
            "payment_references": _ids(payments, "payment_sequential"),
            "shipment_ids": _ids([shipment.get("order_id")]),
        },
        "claim_assessments": assessments,
        "entity_resolution": {
            "status": resolution_status,
            "resolved_order_ids": _ids(resolved),
            "rejected_candidates": _ids(rejected),
            "confidence": 0.95 if resolution_status == "resolved" else 0.25,
        },
        "customer_context": {
            "customer_unique_id": customer.get("customer_unique_id"),
            "related_order_ids": _ids(customer_orders, "order_id"),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": sellers if shipment_verdict == "seller_delay" else [],
            "timeline_complete": bool(shipment),
        },
        "payment_analysis": {
            "verdict": {
                "valid_split_payment": "reconciled",
                "payment_mismatch": "capture_mismatch",
                "duplicate_charge": "duplicate_capture",
                "refund_pending": "refund_pending",
                "refund_failed": "refund_failed",
            }.get(primary, "reconciled" if payment else "insufficient_evidence"),
            "captured_total_brl": captured or None,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refund_amount,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary.upper(), "rank": 1}],
            "responsible_parties": rule.get(
                "responsible_parties", [{"party_type": "unknown", "party_id": None}]
            ),
        },
        "evidence_refs": context.evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": [
                {"reason_code": primary, "amount_brl": refund_amount, "entity_id": None}
            ]
            if refund_amount
            else [],
        },
        "resolution_actions": [rule.get("recommended_action", "investigate_conflicting_evidence")]
        if status != "no_action"
        else ["document_no_action"],
    }
