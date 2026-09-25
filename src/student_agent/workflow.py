from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import ComplaintState
from .trace import TraceWriter

MAX_ITERATIONS = 5
MAX_MCP_ATTEMPTS = 2


def _strings(value: Any) -> list[str]:
    """Normalize scalar/list identifiers without turning absent data into facts."""
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _first(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _ids(data: Any, *names: str) -> list[str]:
    if not isinstance(data, dict):
        return []
    found: list[str] = []
    for name in names:
        found.extend(_strings(data.get(name)))
    return list(dict.fromkeys(found))[:20]


def _number(data: Any, *names: str) -> float | None:
    if not isinstance(data, dict):
        return None
    value = _first(data, *names)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _tool(tools: Iterable[str], *candidates: str) -> str | None:
    """Select only a tool advertised by MCP; never probe unadvertised names."""
    advertised = set(tools)
    for candidate in candidates:
        if candidate in advertised:
            return candidate
    return None


async def _consume(
    state: ComplaintState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    actor: str,
    tool_name: str | None,
    arguments: dict[str, str],
) -> dict[str, Any] | None:
    if tool_name is None:
        return None
    evidence: dict[str, Any] | None = None
    last_error: Exception | None = None
    for _attempt in range(MAX_MCP_ATTEMPTS):
        try:
            evidence = await gateway.call(tool_name, case_id=state["case_id"], **arguments)
            break
        except Exception as exc:  # Retry only the idempotent evidence read once.
            last_error = exc
    if evidence is None:
        error_name = type(last_error).__name__ if last_error else "UnknownError"
        state["errors"] = [*state["errors"], f"{actor}:{error_name}"]
        return None
    state["evidence"] = [*state["evidence"], evidence]
    trace.emit(
        case_id=state["case_id"],
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


def _empty_output(case_id: str) -> dict[str, Any]:
    """A safe, schema-valid fallback. It deliberately makes no business claim."""
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "not_found",
            "resolved_order_ids": [],
            "rejected_candidates": [],
            "confidence": 0.0,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["Route case for manual investigation"],
    }


async def _order_item_worker(
    state: ComplaintState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    case = state["case"]
    supplied = _ids(case, "order_id", "order_ids")
    candidates = _ids(case, "candidate_order_ids", "order_candidates")
    order_id = (supplied or candidates or [""])[0]
    evidence = await _consume(
        state,
        gateway,
        trace,
        actor="order-item-agent",
        tool_name=_tool(
            state["available_tools"], "get_order", "get_order_summary", "resolve_order"
        ),
        arguments={"order_id": order_id} if order_id else {},
    )
    data = evidence.get("data", {}) if evidence else {}
    if isinstance(data, dict):
        state["analysis"]["customer"] = data
    resolved = _ids(data, "order_id", "order_ids", "resolved_order_id") or supplied
    state["resolved_order_ids"] = resolved
    state["rejected_candidates"] = [item for item in candidates if item not in resolved]
    state["entity_confidence"] = 0.9 if evidence and resolved else (0.5 if resolved else 0.0)


async def _investigation_worker(
    state: ComplaintState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    order_id = state["resolved_order_ids"][0] if state["resolved_order_ids"] else ""
    if not order_id:
        return
    for actor, names, key in (
        (
            "shipment-agent",
            ("get_shipment", "get_shipment_summary", "get_order_shipment"),
            "shipment",
        ),
        ("payment-agent", ("get_order_payments", "get_payment_summary", "get_payments"), "payment"),
        ("policy-agent", ("get_policy", "get_refund_policy"), "policy"),
    ):
        trace.emit(
            case_id=state["case_id"], event_type="task_assigned", actor="coordinator", target=actor
        )
        arguments = {"order_id": order_id}
        if actor == "policy-agent":
            arguments = {"policy_type": "standard_complaint"}
        evidence = await _consume(
            state,
            gateway,
            trace,
            actor=actor,
            tool_name=_tool(state["available_tools"], *names),
            arguments=arguments,
        )
        if evidence:
            state["analysis"][key] = evidence["data"]
        state["iteration_count"] += 1
        if state["errors"] or state["iteration_count"] >= MAX_ITERATIONS:
            return


def _build_output(state: ComplaintState) -> dict[str, Any]:
    result = _empty_output(state["case_id"])
    refs = [item["evidence_ref"] for item in state["evidence"]]
    shipment = state["analysis"].get("shipment", {})
    payment = state["analysis"].get("payment", {})
    customer = state["analysis"].get("customer", {})
    result["evidence_refs"] = list(dict.fromkeys(refs))
    result["entity_resolution"] = {
        "status": "resolved" if state["resolved_order_ids"] else "not_found",
        "resolved_order_ids": state["resolved_order_ids"],
        "rejected_candidates": state["rejected_candidates"],
        "confidence": state["entity_confidence"],
    }
    result["affected_entities"]["order_ids"] = state["resolved_order_ids"]
    result["affected_entities"]["seller_ids"] = _ids(shipment, "seller_ids", "seller_id")
    result["affected_entities"]["shipment_ids"] = _ids(shipment, "shipment_ids", "shipment_id")
    result["affected_entities"]["payment_references"] = _ids(
        payment, "payment_references", "payment_id", "payment_ids"
    )
    result["customer_context"] = {
        "customer_unique_id": _first(customer, "customer_unique_id", "customer_id"),
        "related_order_ids": _ids(customer, "related_order_ids", "order_ids"),
    }
    captured = _number(payment, "captured_total_brl", "captured_amount_brl", "paid_total_brl")
    refunded = _number(payment, "refunded_total_brl", "refund_total_brl")
    result["payment_analysis"].update(
        {
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": max((captured or 0) - (refunded or 0), 0)
            if captured is not None
            else None,
        }
    )
    if refs and state["resolved_order_ids"]:
        result["assessment"]["confidence"] = min(0.75, 0.35 + 0.1 * len(refs))
    return result


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run bounded supervisor/worker handoffs and return only audited evidence."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    tools = tuple(await gateway.list_tools())
    state: ComplaintState = {
        "case_id": case_id,
        "case": case,
        "available_tools": tools,
        "current_worker": "order-item-agent",
        "iteration_count": 0,
        "evidence": [],
        "errors": [],
        "resolved_order_ids": [],
        "rejected_candidates": [],
        "entity_confidence": 0.0,
        "analysis": {},
    }
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-item-agent",
    )
    await _order_item_worker(state, gateway, trace)
    state["iteration_count"] += 1
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="investigation-team",
    )
    if state["iteration_count"] < MAX_ITERATIONS and not state["errors"]:
        await _investigation_worker(state, gateway, trace)
    output = _build_output(state)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"],
    )
    return output
