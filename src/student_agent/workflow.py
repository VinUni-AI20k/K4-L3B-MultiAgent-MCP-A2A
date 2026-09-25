from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

MAX_CALLS_PER_CASE = 14
ISSUES = {
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
}
SHIPMENT_VERDICTS = {
    "on_time",
    "seller_delay",
    "logistics_delay",
    "lost",
    "returned",
    "conflicting",
}
PAYMENT_VERDICTS = {
    "reconciled",
    "capture_mismatch",
    "duplicate_capture",
    "refund_pending",
    "refund_failed",
    "refunded",
}
TOOL_PREFERENCES = {
    "order": ("get_order",),
    "customer": ("get_customer_history",),
    "item": ("get_order_items",),
    "product": ("get_product_context",),
    "seller": ("get_sellers",),
    "shipment": ("get_shipment_summary", "get_shipment"),
    "payment": ("get_order_payments", "get_payment"),
    "refund": ("get_refund_timeline", "get_refund"),
    "policy": ("get_policy",),
}


class IssueModel(Protocol):
    async def select_issue(
        self, case: dict[str, Any], candidates: list[str], evidence: list[dict[str, Any]]
    ) -> str: ...


def _find(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        for child in value.values():
            found = _find(child, *names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find(child, *names)
            if found is not None:
                return found
    return None


def _ids(value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))[:20]
    return []


def _all_ids(value: Any, *names: str) -> list[str]:
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in names:
                    found.extend(_ids(child))
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return list(dict.fromkeys(found))[:20]


def _money(value: Any) -> float | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return float(amount.quantize(Decimal("0.01")))


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rows(value: Any, key: str | None = None) -> list[dict[str, Any]]:
    if isinstance(value, dict) and key:
        value = value.get(key)
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _sum_money(rows: list[dict[str, Any]], key: str) -> float | None:
    amounts = [_money(row.get(key)) for row in rows]
    return (
        round(sum(amounts), 2)
        if amounts and all(amount is not None for amount in amounts)
        else None
    )


def _data(evidence: dict[str, Any] | None) -> Any:
    return evidence["data"] if evidence else {}


@dataclass
class Investigation:
    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    tools: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = field(
        default_factory=dict
    )
    consumed: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    async def discover(self) -> None:
        self.tools = await self.gateway.describe_tools()

    async def query(
        self, domain: str, actor: str, preferred_tool: str | None = None, **context: str
    ) -> dict[str, Any] | None:
        allowed = (preferred_tool,) if preferred_tool else TOOL_PREFERENCES.get(domain, ())
        matches = [tool for name in allowed for tool in self.tools if tool["name"] == name]
        for tool in matches:
            schema = tool.get("input_schema") or {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", [])) - {"case_id"}
            arguments = {
                key: value
                for key, value in context.items()
                if value and (key in properties or not properties)
            }
            if not required.issubset(arguments):
                continue
            key = (tool["name"], tuple(sorted(arguments.items())))
            if key in self.cache:
                if self.cache[key] is not None:
                    return self.cache[key]
                continue
            if self.calls >= MAX_CALLS_PER_CASE:
                return None
            self.calls += 1
            try:
                result = await self.gateway.call(tool["name"], case_id=self.case_id, **arguments)
            except (ValueError, KeyError, TimeoutError, RuntimeError):
                self.cache[key] = None
                continue
            data = result.get("data")
            if not isinstance(data, (dict, list)) or _find(data, "case_id") not in (
                None,
                self.case_id,
            ):
                self.cache[key] = None
                continue
            embedded_orders = _all_ids(data, "order_id")
            if (
                context.get("order_id")
                and domain != "customer"
                and embedded_orders
                and any(order != context["order_id"] for order in embedded_orders)
            ):
                self.cache[key] = None
                continue
            ref = result.get("evidence_ref")
            if (
                result.get("domain") != domain
                or not isinstance(ref, str)
                or not ref.startswith("ev_")
            ):
                self.cache[key] = None
                continue
            self.cache[key] = result
            self.consumed[ref] = result
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool["name"],
                evidence_refs=[ref],
            )
            return result
        return None


async def _resolve(inv: Investigation) -> tuple[str, list[str], list[str], str | None, float]:
    request = inv.case.get("customer_request", {})
    claimed = request.get("claimed_order_id")
    hint = inv.case.get("customer_unique_id_hint")
    candidates = _ids(inv.case.get("candidate_order_ids"))
    if claimed and claimed not in candidates:
        candidates.insert(0, claimed)
    scored: list[tuple[str, int, str | None]] = []
    for candidate in candidates[:4]:
        evidence = await inv.query("order", "entity-agent", order_id=candidate)
        data = _data(evidence)
        if _find(data, "order_id") != candidate:
            continue
        customer = _find(data, "customer_unique_id")
        if hint and customer and hint != customer:
            scored.append((candidate, -1, customer))
            continue
        score = 1 + int(candidate == claimed) + int(bool(hint and customer == hint))
        scored.append((candidate, score, customer))
        if score == 3:
            break
    positive = sorted((row for row in scored if row[1] > 0), key=lambda row: row[1], reverse=True)
    if not positive:
        return "not_found", [], [row[0] for row in scored if row[1] < 0], None, 0.0
    if len(positive) > 1 and positive[0][1] == positive[1][1]:
        return "ambiguous", [], [row[0] for row in scored if row[1] < 0], None, 0.0
    winner = positive[0]
    rejected = [row[0] for row in scored if row[0] != winner[0] and row[1] < winner[1]]
    return "resolved", [winner[0]], rejected, winner[2], min(0.95, 0.5 + winner[1] * 0.15)


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    model: IssueModel | None = None,
) -> dict[str, Any]:
    inv = Investigation(case, gateway, trace)
    await inv.discover()
    trace.emit(
        case_id=inv.case_id, event_type="task_assigned", actor="coordinator", target="entity-agent"
    )
    entity_status, resolved, rejected, customer_id, entity_confidence = await _resolve(inv)
    trace.emit(
        case_id=inv.case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="resolved" if resolved else "unresolved",
    )

    order_id = resolved[0] if resolved else None
    evidence_by_domain: dict[str, dict[str, Any] | None] = {}
    if order_id:
        for domain, actor in (
            ("customer", "customer-agent"),
            ("item", "order-agent"),
            ("shipment", "shipment-agent"),
            ("payment_timeline", "payment-agent"),
            ("refund", "payment-agent"),
            ("policy", "policy-agent"),
        ):
            scope = case.get("investigation_scope", {})
            if domain == "customer" and not scope.get("include_customer_history", True):
                continue
            context = {
                "order_id": order_id,
                "customer_unique_id": customer_id or case.get("customer_unique_id_hint", ""),
                "policy_version": case.get("policy_version", ""),
            }
            trace.emit(
                case_id=inv.case_id, event_type="task_assigned", actor="coordinator", target=actor
            )
            if domain == "payment_timeline":
                evidence_by_domain[domain] = await inv.query(
                    "payment", actor, preferred_tool="get_payment_timeline", **context
                )
                timeline_data = _data(evidence_by_domain[domain])
                has_payment_rows = bool(_rows(timeline_data, "payments"))
                has_payment_total = _find(
                    timeline_data, "captured_total_brl", "captured_amount_brl"
                ) is not None
                if evidence_by_domain[domain] is None or not (
                    has_payment_rows or has_payment_total
                ):
                    evidence_by_domain["payment"] = await inv.query(
                        "payment", actor, **context
                    )
            else:
                evidence_by_domain[domain] = await inv.query(domain, actor, **context)
            trace.emit(case_id=inv.case_id, event_type="handoff", actor=actor, target="coordinator")

        if not _all_ids(_data(evidence_by_domain.get("item")), "seller_ids", "seller_id"):
            trace.emit(
                case_id=inv.case_id,
                event_type="task_assigned",
                actor="coordinator",
                target="order-agent",
            )
            evidence_by_domain["seller"] = await inv.query(
                "seller", "order-agent", order_id=order_id
            )
            trace.emit(
                case_id=inv.case_id, event_type="handoff", actor="order-agent", target="coordinator"
            )

    order = next(
        (
            result
            for result in inv.consumed.values()
            if result.get("domain") == "order" and _find(result.get("data"), "order_id") == order_id
        ),
        None,
    )
    shipment = _data(evidence_by_domain.get("shipment"))
    payment_timeline = _data(evidence_by_domain.get("payment_timeline"))
    payment = _data(evidence_by_domain.get("payment") or evidence_by_domain.get("payment_timeline"))
    refund = _data(evidence_by_domain.get("refund"))
    policy = _data(evidence_by_domain.get("policy"))
    item = _data(evidence_by_domain.get("item"))
    seller = _data(evidence_by_domain.get("seller"))
    customer = _data(evidence_by_domain.get("customer"))
    order_data = _data(order)

    customer_orders = _all_ids(customer, "order_id")
    if order_id and order_id in customer_orders:
        customer_id = _find(customer, "customer_unique_id") or customer_id
        entity_confidence = min(0.95, entity_confidence + 0.1)

    shipment_events = _rows(shipment, "events")
    payment_rows = _rows(payment) or _rows(payment_timeline, "payments")
    payment_events = _rows(payment_timeline, "events")
    refund_events = _rows(refund, "events")
    delivery = _date(_find(shipment, "delivered_customer_at", "delivered_at", "delivery_date"))
    expected = _date(_find(shipment, "estimated_delivery_at", "estimated_delivery_date"))
    carrier = _date(_find(shipment, "delivered_carrier_at"))
    late_sellers = _all_ids(shipment, "late_seller_ids")
    if carrier:
        late_sellers = list(
            dict.fromkeys(
                late_sellers
                + [
                    row["seller_id"]
                    for row in _rows(shipment, "shipping_limits")
                    if isinstance(row.get("seller_id"), str)
                    and (limit := _date(row.get("shipping_limit_at")))
                    and carrier > limit
                ]
            )
        )[:20]
    shipment_verdict = _find(shipment, "verdict", "shipment_verdict")
    if shipment_verdict not in SHIPMENT_VERDICTS:
        late_event = next(
            (event for event in shipment_events if event.get("event_type") == "delivered_late"),
            None,
        )
        if late_event and late_event.get("actor") == "seller":
            shipment_verdict = "seller_delay"
        elif late_event and late_event.get("actor") == "logistics_provider":
            shipment_verdict = "logistics_delay"
        elif delivery and expected:
            shipment_verdict = (
                "on_time"
                if delivery <= expected
                else "seller_delay"
                if late_sellers
                else "logistics_delay"
            )
        else:
            shipment_verdict = "insufficient_evidence"

    captured = _money(_find(payment, "captured_total_brl", "captured_amount_brl"))
    if captured is None:
        captured = _sum_money(payment_rows, "payment_value")
    if captured is None:
        captured = _sum_money(
            [event for event in payment_events if event.get("event_type") == "captured"],
            "amount_brl",
        )
    refunded = _money(_find(refund, "refunded_total_brl", "refunded_amount_brl"))
    if refunded is None:
        refunded = _sum_money(
            [
                event
                for event in refund_events
                if event.get("status") in {"completed", "confirmed", "refunded"}
            ],
            "amount_brl",
        )
    payment_verdict = _find(payment, "verdict", "payment_verdict") or _find(
        refund, "verdict", "refund_verdict"
    )
    event_types = {event.get("event_type") for event in payment_events}
    refund_statuses = {event.get("status") for event in refund_events}
    payment_signatures = [
        (row.get("payment_type"), _money(row.get("payment_value"))) for row in payment_rows
    ]
    duplicate = len(payment_signatures) >= 3 and len(set(payment_signatures)) < len(
        payment_signatures
    )
    order_status = _find(order_data, "order_status")
    derived = []
    if "failed" in refund_statuses:
        derived.append("refund_failed")
    elif "pending" in refund_statuses:
        derived.append("refund_pending")
    if order_status == "canceled" and captured is not None and captured > 0:
        derived.append("canceled_order_paid")
    elif order_status == "unavailable" and captured is not None and captured > 0:
        derived.append("unavailable_order_paid")
    if "reconciliation_mismatch" in event_types:
        derived.append("payment_mismatch")
    elif duplicate:
        derived.append("duplicate_charge")
    if shipment_verdict == "seller_delay":
        derived.append("late_delivery_seller")
    elif shipment_verdict == "logistics_delay":
        derived.append("late_delivery_logistics")
    claim_topics = [
        claim.get("topic")
        for claim in case.get("customer_request", {}).get("claims", [])
        if isinstance(claim, dict)
    ]
    if not derived and "valid_split_payment" in claim_topics and len(payment_rows) > 1:
        derived.append("valid_split_payment")
    if not derived and "unsupported_claim" in claim_topics and order_data:
        derived.append("unsupported_claim")
    candidates = list(
        dict.fromkeys(
            value
            for value in (
                _find(order_data, "primary_issue", "issue_code"),
                _find(shipment, "primary_issue", "issue_code"),
                _find(payment, "primary_issue", "issue_code"),
                _find(payment_timeline, "primary_issue", "issue_code"),
                _find(refund, "primary_issue", "issue_code"),
                payment_verdict,
                *derived,
            )
            if value in ISSUES
        )
    )
    issue = candidates[0] if resolved and candidates else "insufficient_evidence"
    if resolved and len(candidates) > 1:
        if model is not None:
            selected = await model.select_issue(case, candidates, list(inv.consumed.values()))
            if selected not in candidates:
                raise ValueError("model selected an issue outside the evidence candidates")
            issue = selected
        trace.emit(
            case_id=inv.case_id,
            event_type="handoff",
            actor="conflict-resolver",
            target="coordinator",
            decision_code="evidence_backed_issue_selected",
        )
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    rule = rules.get(issue, {}) if isinstance(rules, dict) else {}
    status = rule.get("case_status", "needs_investigation")
    if status not in {"action_required", "no_action", "needs_investigation"}:
        status = "needs_investigation"
    if not rule and issue in {"valid_split_payment", "unsupported_claim"}:
        status = "no_action"
    confidence = min(entity_confidence, 0.85 if policy else 0.6) if resolved else 0.0
    if issue == "insufficient_evidence":
        confidence = min(confidence, 0.3)
    refundable = _money(rule.get("refund_brl")) if rule else None
    recommended = refundable if status == "action_required" and refundable else 0.0
    if payment_verdict not in PAYMENT_VERDICTS:
        payment_verdict = (
            "refund_failed"
            if issue == "refund_failed"
            else "refund_pending"
            if issue == "refund_pending"
            else "capture_mismatch"
            if issue == "payment_mismatch"
            else "duplicate_capture"
            if issue == "duplicate_charge"
            else "reconciled"
            if captured is not None
            else "insufficient_evidence"
        )
    evidence_refs = list(inv.consumed)[:30]
    topic_claims = case.get("customer_request", {}).get("claims", [])
    source_refs = {
        domain: result["evidence_ref"]
        for domain, result in evidence_by_domain.items()
        if result is not None
    }
    if order:
        source_refs["order"] = order["evidence_ref"]
    issue_domains = (
        ("shipment", "order", "item")
        if issue.startswith("late_delivery")
        else ("refund", "payment_timeline", "payment")
        if issue.startswith("refund")
        else ("payment_timeline", "payment", "item")
        if issue in {"duplicate_charge", "payment_mismatch", "valid_split_payment"}
        else ("order", "payment_timeline", "payment")
        if issue in {"canceled_order_paid", "unavailable_order_paid"}
        else ("order", "shipment", "payment_timeline")
    )
    supported_shipment = (
        issue == "late_delivery_seller"
        and shipment_verdict == "seller_delay"
        or issue == "late_delivery_logistics"
        and shipment_verdict == "logistics_delay"
    )
    issue_refs = []
    for domain in issue_domains:
        if domain not in source_refs:
            continue
        if issue.startswith("late_delivery"):
            if domain in {"shipment", "item"} and not supported_shipment:
                continue
            if domain == "order" and _find(order_data, "primary_issue", "issue_code") != issue:
                continue
        issue_refs.append(source_refs[domain])
    issue_refs = list(dict.fromkeys(issue_refs))
    if "policy" in source_refs:
        issue_refs.append(source_refs["policy"])
    claim_assessments = []
    for claim in topic_claims[:5]:
        if not isinstance(claim, dict) or not claim.get("claim_id"):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            verdict = (
                "supported"
                if captured is not None and captured > 0 and recommended >= captured
                else "partially_supported"
                if recommended > 0
                else "unsupported"
                if issue != "insufficient_evidence" and policy
                else "insufficient_evidence"
            )
            claim_refs = [
                source_refs[domain]
                for domain in ("payment_timeline", "payment", "policy")
                if domain in source_refs
            ]
        else:
            verdict = (
                "supported"
                if topic == issue
                else "unsupported"
                if issue != "insufficient_evidence" and issue_refs
                else "insufficient_evidence"
            )
            claim_refs = issue_refs if verdict != "insufficient_evidence" else []
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": min(confidence, 0.85) if claim_refs else 0.0,
                "evidence_refs": list(dict.fromkeys(claim_refs))[:30],
            }
        )
    parties = rule.get("responsible_parties", []) if rule else []
    parties = [
        party
        for party in parties
        if isinstance(party, dict)
        and party.get("party_type")
        in {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
    ][:5]
    action = rule.get("recommended_action") if rule else None
    order_shipment_status = _find(shipment, "order_status")
    conflicts = (
        [
            {
                "field": "order_status",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_order",
                "resolution_code": "prefer_order_record",
            }
        ]
        if order_status and order_shipment_status and order_status != order_shipment_status
        else []
    )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": inv.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [candidate for candidate in candidates if candidate != issue],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": resolved,
            "item_ids": _all_ids(item, "item_ids", "item_id", "order_item_id"),
            "seller_ids": list(
                dict.fromkeys(
                    _all_ids(seller, "seller_ids", "seller_id")
                    + _all_ids(item, "seller_ids", "seller_id")
                )
            )[:20],
            "payment_references": _all_ids(payment, "payment_references", "payment_reference"),
            "shipment_ids": _all_ids(shipment, "shipment_ids", "shipment_id"),
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved,
            "rejected_candidates": rejected,
            "confidence": entity_confidence,
        },
        "customer_context": {
            "customer_unique_id": customer_id if resolved else None,
            "related_order_ids": _all_ids(customer, "related_order_ids", "order_id"),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": bool(_find(shipment, "timeline_complete"))
            or bool(
                carrier and expected and (delivery or order_status in {"canceled", "unavailable"})
            ),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": []
            if issue == "insufficient_evidence"
            else [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended,
            "refund_lines": []
            if not recommended
            else [
                {
                    "reason_code": action.upper() if isinstance(action, str) else "POLICY_REFUND",
                    "amount_brl": recommended,
                    "entity_id": order_id,
                }
            ],
        },
        "resolution_actions": [action] if isinstance(action, str) and action else [],
    }
    trace.contracts.validate_output(output, f"outputs/{inv.case_id}.json")
    trace.emit(
        case_id=inv.case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="schema_and_scope_checked",
        attributes={"tool_calls": inv.calls, "evidence_count": len(evidence_refs)},
    )
    return output
