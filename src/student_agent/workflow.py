"""Evidence-scoped multi-agent workflow for the L3B complaint cases."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .investigators import (
    EntityAgent,
    EvidenceBroker,
    Handoff,
    OrderAgent,
    PaymentAgent,
    PolicyAgent,
    ShipmentAgent,
)
from .mcp_gateway import EvidenceGateway
from .model_agent import ModelVerifier
from .trace import TraceWriter


def _unique(*groups: list[str]) -> list[str]:
    return list(dict.fromkeys(ref for group in groups for ref in group if ref))


def _handoff(trace: TraceWriter, case_id: str, actor: str, handoff: Handoff) -> None:
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        evidence_refs=handoff.refs,
    )


def _issues(
    entity: dict[str, Any], payment: dict[str, Any], shipment: dict[str, Any]
) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    status = str(entity["order"].get("order_status", "")).lower()
    if status == "canceled" and (payment["captured"] or 0) > 0:
        found.append(("canceled_order_paid", 100))
    if status == "unavailable" and (payment["captured"] or 0) > 0:
        found.append(("unavailable_order_paid", 100))
    payment_issue = {
        "refund_failed": ("refund_failed", 95),
        "refund_pending": ("refund_pending", 90),
        "duplicate_capture": ("duplicate_charge", 78),
        "capture_mismatch": ("payment_mismatch", 72 if payment["mismatch_event"] else 42),
    }.get(payment["verdict"])
    if payment_issue:
        found.append(payment_issue)
    shipment_issue = {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
    }.get(shipment["verdict"])
    if shipment_issue:
        strength = 86 if shipment["events"] else 64
        found.append((shipment_issue, strength))
    if not found:
        if payment["verdict"] == "reconciled" and len(payment["selected_payments"]) > 1:
            found.append(("valid_split_payment", 68))
        elif payment["verdict"] != "insufficient_evidence" and shipment["verdict"] == "on_time":
            found.append(("unsupported_claim", 62))
        else:
            found.append(("insufficient_evidence", 25))
    return sorted(found, key=lambda item: (-item[1], item[0]))


def _party(
    issue: str, rule: dict[str, Any], shipment: dict[str, Any]
) -> list[dict[str, str | None]]:
    parties = rule.get("responsible_parties")
    if not isinstance(parties, list):
        parties = []
    valid = []
    for party in parties:
        if not isinstance(party, dict) or party.get("party_type") not in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }:
            continue
        party_type = party["party_type"]
        party_id = party.get("party_id") if isinstance(party.get("party_id"), str) else None
        if party_type == "seller":
            party_id = (shipment["late_seller_ids"] or shipment["seller_ids"] or [None])[0]
        valid.append({"party_type": party_type, "party_id": party_id})
    if valid:
        return valid[:5]
    fallback = (
        "seller"
        if issue == "late_delivery_seller"
        else "logistics_provider"
        if issue == "late_delivery_logistics"
        else "payment_provider"
        if issue in {"duplicate_charge", "payment_mismatch", "refund_pending", "refund_failed"}
        else "platform"
        if issue in {"canceled_order_paid", "unavailable_order_paid"}
        else "customer"
        if issue in {"valid_split_payment", "unsupported_claim"}
        else "unknown"
    )
    seller_id = (shipment["late_seller_ids"] or shipment["seller_ids"] or [None])[0]
    return [{"party_type": fallback, "party_id": seller_id if fallback == "seller" else None}]


def _conflicts(
    evidence: EvidenceBroker,
    entity: dict[str, Any],
    order: dict[str, Any],
    payment: dict[str, Any],
    shipment: dict[str, Any],
) -> list[dict[str, Any]]:
    conflicts = []
    if entity["order"].get("order_status") != shipment["order_status"] and shipment["order_status"]:
        conflicts.append(
            {
                "field": "order_status",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_order",
                "resolution_code": "ORDER_SNAPSHOT_PRIORITY",
            }
        )
    payment_response = next(
        (value for (tool, _), value in evidence.results.items() if tool == "get_order_payments"),
        None,
    )
    if payment_response and payment["captured"] is not None:
        raw_values = [
            Decimal(str(row["payment_value"]))
            for row in payment_response.get("data", [])
            if isinstance(row, dict) and row.get("payment_value") is not None
        ]
        if raw_values and sum(raw_values) != Decimal(str(payment["captured"])):
            conflicts.append(
                {
                    "field": "captured_total_brl",
                    "sources": ["get_order_payments", "get_payment_timeline"],
                    "selected_source": "get_payment_timeline",
                    "resolution_code": "DATED_CONFIRMED_EVENTS",
                }
            )
    if len(order["purchases"]) > 1:
        conflicts.append(
            {
                "field": "order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_order",
                "resolution_code": "PURCHASE_EPISODE_MATCH",
            }
        )
    return conflicts[:5]


def _claim_refs(topic: str, evidence: EvidenceBroker) -> list[str]:
    if topic == "requested_full_refund":
        return evidence.refs(
            "get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"
        )
    if topic in {"late_delivery_seller", "late_delivery_logistics"}:
        return evidence.refs("get_order", "get_shipment_summary", "get_sellers")
    if topic in {
        "payment_mismatch",
        "duplicate_charge",
        "valid_split_payment",
        "refund_pending",
        "refund_failed",
    }:
        return evidence.refs(
            "get_order",
            "get_order_items",
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
        )
    if topic in {"canceled_order_paid", "unavailable_order_paid"}:
        return evidence.refs("get_order", "get_payment_timeline", "get_shipment_summary")
    return evidence.refs("get_order", "get_payment_timeline", "get_shipment_summary")


def _claims(
    case: dict[str, Any],
    issue: str,
    secondary: list[str],
    refund: float,
    captured: float | None,
    evidence: EvidenceBroker,
) -> list[dict[str, Any]]:
    assessments = []
    for claim in case.get("customer_request", {}).get("claims", []):
        if not isinstance(claim, dict):
            continue
        topic = str(claim.get("topic", ""))
        if topic == "requested_full_refund":
            if captured is None:
                verdict = "insufficient_evidence"
            elif refund >= captured > 0:
                verdict = "supported"
            elif refund > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == issue or topic in secondary:
            verdict = "supported"
        else:
            verdict = "unsupported"
        assessments.append(
            {
                "claim_id": str(claim.get("claim_id", "claim")),
                "verdict": verdict,
                "confidence": 0.86 if verdict in {"supported", "unsupported"} else 0.68,
                "evidence_refs": _claim_refs(topic, evidence),
            }
        )
    return assessments[:5]


def _verify(output: dict[str, Any], evidence: EvidenceBroker) -> None:
    submitted = output["evidence_refs"]
    audited = {value["evidence_ref"] for value in evidence.results.values()}
    if not submitted or len(submitted) != len(set(submitted)) or not set(submitted) <= audited:
        raise ValueError("output contains missing, duplicate or unaudited evidence refs")
    if (
        output["entity_resolution"]["status"] == "resolved"
        and output["affected_entities"]["order_ids"]
        != output["entity_resolution"]["resolved_order_ids"]
    ):
        raise ValueError("resolved order and affected order disagree")
    financial = output["financial_resolution"]
    total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if total != round(financial["recommended_refund_brl"], 2):
        raise ValueError("refund lines do not add up")
    if (
        financial["recommended_refund_brl"] > 0
        and output["assessment"]["case_status"] != "action_required"
    ):
        raise ValueError("positive refund requires action_required")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= set(submitted):
            raise ValueError("claim evidence is missing from output evidence")


def _unresolved(case: dict[str, Any], entity: Handoff) -> dict[str, Any]:
    case_id = case["case_id"]
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.18,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": str(claim["claim_id"]),
                "verdict": "insufficient_evidence",
                "confidence": 0.18,
                "evidence_refs": entity.refs,
            }
            for claim in case.get("customer_request", {}).get("claims", [])
            if isinstance(claim, dict) and claim.get("claim_id")
        ],
        "entity_resolution": {
            "status": entity.facts["status"],
            "resolved_order_ids": [],
            "rejected_candidates": entity.facts["rejected_candidates"],
            "confidence": entity.facts["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity.facts["customer_unique_id"],
            "related_order_ids": entity.facts["related_order_ids"],
        },
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
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": entity.refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["investigate_entity_resolution"],
    }


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    model: ModelVerifier,
    tools: set[str],
) -> dict[str, Any]:
    """Coordinate specialists, a <=10B model verifier and deterministic checks."""
    case_id = case["case_id"]
    evidence = EvidenceBroker(gateway, trace, case_id, tools)
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="entity-agent"
    )
    entity_handoff = await EntityAgent(evidence).investigate(case)
    _handoff(trace, case_id, "entity-agent", entity_handoff)
    entity = entity_handoff.facts
    if entity["status"] != "resolved":
        output = _unresolved(case, entity_handoff)
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="ENTITY_UNRESOLVED",
            evidence_refs=entity_handoff.refs,
        )
        return output

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="order-agent"
    )
    order_handoff = await OrderAgent(evidence).investigate(case, entity)
    _handoff(trace, case_id, "order-agent", order_handoff)
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="payment-agent"
    )
    payment_handoff = await PaymentAgent(evidence).investigate(
        entity["order_id"], order_handoff.facts
    )
    _handoff(trace, case_id, "payment-agent", payment_handoff)
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="shipment-agent"
    )
    shipment_handoff = await ShipmentAgent(evidence).investigate(
        entity["order_id"], order_handoff.facts
    )
    _handoff(trace, case_id, "shipment-agent", shipment_handoff)
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="policy-agent"
    )
    policy_handoff = await PolicyAgent(evidence).investigate(str(case.get("policy_version", "")))
    _handoff(trace, case_id, "policy-agent", policy_handoff)

    order, payment, shipment = order_handoff.facts, payment_handoff.facts, shipment_handoff.facts
    candidates = _issues(entity, payment, shipment)
    review_facts = {
        "case_id": case_id,
        "candidate_issues": [issue for issue, _ in candidates],
        "order_status": entity["order"].get("order_status"),
        "purchase_at": entity["order"].get("order_purchase_timestamp"),
        "item_total_brl": order["total"],
        "payment_verdict": payment["verdict"],
        "captured_total_brl": payment["captured"],
        "payment_events": [
            {
                "type": event.get("event_type"),
                "status": event.get("status"),
                "amount": event.get("amount_brl"),
            }
            for event in payment["events"]
        ],
        "refund_events": [
            {
                "type": event.get("event_type"),
                "status": event.get("status"),
                "amount": event.get("amount_brl"),
            }
            for event in payment["refund_events"]
        ],
        "shipment_verdict": shipment["verdict"],
        "shipment_events": [
            {"type": event.get("event_type"), "actor": event.get("actor")}
            for event in shipment["events"]
        ],
    }
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="model-verifier"
    )
    model_result = None
    model_replied = False
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            candidate_result = await model.review(review_facts)
        except Exception as exc:
            last_error = exc
            continue
        model_replied = True
        if candidate_result.get("primary_issue") in review_facts["candidate_issues"]:
            model_result = candidate_result
            break
    if not model_replied:
        raise RuntimeError(
            f"{case_id}: small-model verifier unavailable ({type(last_error).__name__})"
        ) from last_error
    top_issue, top_score = candidates[0]
    model_valid = model_result is not None
    model_issue = model_result["primary_issue"] if model_result else top_issue
    model_score = next(score for issue, score in candidates if issue == model_issue)
    issue = model_issue if top_score - model_score <= 15 else top_issue
    secondary = [other for other, score in candidates if other != issue and score >= 60][:5]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="model-verifier",
        target="coordinator",
        decision_code=issue if model_valid else "MODEL_OUT_OF_SCOPE",
        evidence_refs=_unique(entity_handoff.refs, payment_handoff.refs, shipment_handoff.refs),
        attributes={"model_valid": model_valid},
    )

    policy = policy_handoff.facts
    rules = policy.get("rules") if isinstance(policy.get("rules"), dict) else {}
    rule = rules.get(issue) if isinstance(rules.get(issue), dict) else {}
    policy_available = bool(rule)
    refund = float(rule.get("refund_brl", 0)) if policy_available else 0.0
    available = payment["refundable"]
    if available is not None:
        refund = max(0.0, min(refund, available))
    action = (
        str(rule.get("recommended_action", "investigate_policy"))
        if policy_available
        else "investigate_policy"
    )
    status = (
        str(rule.get("case_status", "needs_investigation"))
        if policy_available
        else "needs_investigation"
    )
    if status not in {"action_required", "no_action", "needs_investigation"}:
        status = "needs_investigation"
    if refund > 0 and status != "action_required":
        status = "action_required"
    confidence = 0.94 if top_score >= 85 else 0.87 if top_score >= 60 else 0.72
    if issue != top_issue:
        confidence -= 0.12
    if not model_valid:
        confidence -= 0.1
    if not policy_available or evidence.failures & {
        "get_order",
        "get_payment_timeline",
        "get_shipment_summary",
    }:
        confidence -= 0.2
    confidence = round(max(0.2, min(0.97, confidence)), 2)
    refs = _unique(
        entity_handoff.refs,
        order_handoff.refs,
        payment_handoff.refs,
        shipment_handoff.refs,
        policy_handoff.refs,
    )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": secondary,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [entity["order_id"]],
            "item_ids": order["item_ids"],
            "seller_ids": shipment["seller_ids"],
            "payment_references": payment["payment_references"],
            "shipment_ids": shipment["shipment_ids"],
        },
        "claim_assessments": _claims(case, issue, secondary, refund, payment["captured"], evidence),
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": [entity["order_id"]],
            "rejected_candidates": entity["rejected_candidates"],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": entity["related_order_ids"],
        },
        "shipment_analysis": {
            "verdict": shipment["verdict"],
            "late_seller_ids": shipment["late_seller_ids"],
            "timeline_complete": shipment["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment["verdict"],
            "captured_total_brl": payment["captured"],
            "refunded_total_brl": payment["refunded"],
            "refundable_total_brl": payment["refundable"],
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": cause.upper(), "rank": index + 1}
                for index, cause in enumerate([issue, *secondary][:5])
            ],
            "responsible_parties": _party(issue, rule, shipment),
        },
        "evidence_refs": refs,
        "data_conflicts": _conflicts(evidence, entity, order, payment, shipment),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": round(refund, 2),
            "refund_lines": [
                {
                    "reason_code": issue.upper(),
                    "amount_brl": round(refund, 2),
                    "entity_id": entity["order_id"],
                }
            ]
            if refund > 0
            else [],
        },
        "resolution_actions": [action],
    }
    _verify(output, evidence)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="EVIDENCE_LINKED",
        evidence_refs=refs,
    )
    return output
