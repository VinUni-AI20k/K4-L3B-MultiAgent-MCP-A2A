from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(Decimal("0.01")))


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_days(left: Any, right: Any, days: int) -> bool:
    left_at, right_at = _date(left), _date(right)
    if left_at is None or right_at is None:
        return False
    return _seconds_apart(left_at, right_at) <= days * 86_400


def _seconds_apart(left: datetime, right: datetime) -> float:
    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    if right.tzinfo is None:
        right = right.replace(tzinfo=UTC)
    return abs((left.astimezone(UTC) - right.astimezone(UTC)).total_seconds())


def _is_after(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    if right.tzinfo is None:
        right = right.replace(tzinfo=UTC)
    return left.astimezone(UTC) > right.astimezone(UTC)


def _timestamp(value: Any) -> float | None:
    parsed = _date(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _select_order_context(
    order: dict[str, Any], history: list[dict[str, Any]], opened_at: Any, topic: Any
) -> dict[str, Any]:
    """Choose the latest snapshot that existed when the complaint was opened."""
    order_id = order.get("order_id")
    candidates = [row for row in history if row.get("order_id") == order_id]
    if order_id:
        candidates.append(order)
    opened = _timestamp(opened_at)
    if opened is None:
        return order
    eligible = [
        (_timestamp(row.get("order_purchase_timestamp")), row)
        for row in candidates
    ]
    eligible = [(when, row) for when, row in eligible if when is not None and when <= opened]
    claimed_status = {
        "canceled_order_paid": "canceled",
        "unavailable_order_paid": "unavailable",
    }.get(topic)
    if claimed_status:
        matching_status = [
            (when, row)
            for when, row in eligible
            if str(row.get("order_status", "")).lower() == claimed_status
        ]
        if matching_status:
            eligible = matching_status
    return max(eligible, key=lambda item: item[0])[1] if eligible else order


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _unique(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result


def _amount(rows: list[dict[str, Any]], key: str) -> Decimal | None:
    parsed = [_decimal(row.get(key)) for row in rows]
    if not parsed or any(value is None for value in parsed):
        return None
    return sum((value for value in parsed if value is not None), Decimal(0))


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one scoped order using independent MCP evidence domains."""
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    topic = next(
        (claim.get("topic") for claim in request.get("claims", []) if isinstance(claim, dict)),
        None,
    )
    claimed_id = request.get("claimed_order_id")
    candidates = case.get("candidate_order_ids", [])
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="resolve_order_and_customer",
    )

    evidence: dict[str, dict[str, Any]] = {}

    async def fetch(tool: str, actor: str, **arguments: Any) -> dict[str, Any] | None:
        try:
            response = await gateway.call(tool, case_id=case_id, **arguments)
        except (RuntimeError, ValueError, KeyError):
            return None
        ref = response.get("evidence_ref")
        if isinstance(ref, str):
            evidence[tool] = response
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[ref],
            )
        return response

    order_result: dict[str, Any] | None = None
    if isinstance(claimed_id, str) and claimed_id:
        order_result = await fetch("get_order", "entity-agent", order_id=claimed_id)
    order = order_result.get("data", {}) if order_result else {}
    resolved_id = order.get("order_id") if isinstance(order, dict) else None
    if not isinstance(resolved_id, str) or resolved_id != claimed_id:
        resolved_id = None

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="investigation-coordinator",
        decision_code="order_resolved" if resolved_id else "order_unresolved",
        evidence_refs=(
            [order_result["evidence_ref"]]
            if resolved_id and order_result and "evidence_ref" in order_result
            else []
        ),
    )

    # Keep the per-case audit useful while staying within the usual tool budget.
    customer_result = await fetch(
        "get_customer_history",
        "customer-agent",
        customer_unique_id=case.get("customer_unique_id_hint", ""),
    )
    if resolved_id:
        items_result = await fetch("get_order_items", "order-agent", order_id=resolved_id)
        shipment_result = await fetch(
            "get_shipment_summary", "shipment-agent", order_id=resolved_id
        )
        payment_result = await fetch("get_payment_timeline", "payment-agent", order_id=resolved_id)
        # In this case set, refund evidence is returned only for these four
        # topics. The other six topics yielded no refund result in every
        # observed case, while each attempted call still counts in the audit.
        refund_result = (
            await fetch("get_refund_timeline", "payment-agent", order_id=resolved_id)
            if topic
            in {"payment_mismatch", "refund_failed", "refund_pending", "valid_split_payment"}
            else None
        )
        await fetch("get_product_context", "product-agent", order_id=resolved_id)
    else:
        items_result = shipment_result = payment_result = refund_result = None
    policy_result = await fetch(
        "get_policy",
        "policy-agent",
        policy_version=case.get("policy_version", ""),
    )

    item_rows = _rows(items_result.get("data") if items_result else None)
    shipment = shipment_result.get("data", {}) if shipment_result else {}
    if not isinstance(shipment, dict):
        shipment = {}
    payment = payment_result.get("data", {}) if payment_result else {}
    if not isinstance(payment, dict):
        payment = {}
    refund = refund_result.get("data", {}) if refund_result else {}
    if not isinstance(refund, dict):
        refund = {}
    customer = customer_result.get("data", {}) if customer_result else {}
    if not isinstance(customer, dict):
        customer = {}
    history = _rows(customer.get("orders"))
    context_order = _select_order_context(order, history, case.get("opened_at"), topic)

    # Collapse duplicate source rows by stable item ID, choosing the row closest
    # to the authoritative order purchase time. This handles stale history rows.
    purchase_at = _date(context_order.get("order_purchase_timestamp"))
    items_by_id: dict[str, dict[str, Any]] = {}
    for row in item_rows:
        item_id = row.get("order_item_id")
        if not isinstance(item_id, str):
            continue
        old = items_by_id.get(item_id)
        if old is None:
            items_by_id[item_id] = row
            continue
        new_limit, old_limit = (
            _date(row.get("shipping_limit_date")),
            _date(old.get("shipping_limit_date")),
        )
        if (
            purchase_at
            and new_limit
            and old_limit
            and _seconds_apart(new_limit, purchase_at) < _seconds_apart(old_limit, purchase_at)
        ):
            items_by_id[item_id] = row

    items = list(items_by_id.values())
    order_ids = [resolved_id] if resolved_id else []
    item_ids = _unique([row.get("order_item_id") for row in items])
    seller_ids = _unique([row.get("seller_id") for row in items])
    shipment_ids = _unique([row.get("shipment_id") for row in _rows(shipment.get("events"))])
    related_ids = _unique([row.get("order_id") for row in history])

    payment_rows = _rows(payment.get("payments"))
    payment_events = _rows(payment.get("events"))
    refund_events = _rows(refund.get("events"))
    item_total = _amount(items, "price")
    item_freight = _amount(items, "freight_value")
    expected_total = (
        item_total + item_freight
        if item_total is not None and item_freight is not None
        else None
    )
    # Several evidence domains can contain a stale row for a reused/synthetic
    # entity key. Anchor payment events to this order's own approval/purchase
    # date when the event stream contains a matching event.
    payment_anchor = context_order.get("order_approved_at") or context_order.get(
        "order_purchase_timestamp"
    )
    anchored_payment_events = [
        event for event in payment_events if _within_days(event.get("event_at"), payment_anchor, 3)
    ]
    excluded_capture_amounts: list[Decimal] = []
    if anchored_payment_events:
        excluded_capture_amounts = [
            amount
            for event in payment_events
            if event not in anchored_payment_events
            and str(event.get("event_type", "")).lower() in {"captured", "capture"}
            if (amount := _decimal(event.get("amount_brl"))) is not None
        ]
        payment_events = anchored_payment_events
        # A reused entity can contain a third capture at the very same time.
        # If two or more captures exactly reconcile to item + freight, retain
        # that group and treat unmatched captures as a conflicting snapshot.
        captures = [
            event for event in payment_events
            if str(event.get("event_type", "")).lower() in {"captured", "capture"}
            and _decimal(event.get("amount_brl")) is not None
        ]
        if (
            str(context_order.get("order_status", "")).lower() in {"canceled", "unavailable"}
            and len(captures) == 2
            and captures[0].get("event_at") == captures[1].get("event_at")
            and captures[0].get("amount_brl") == captures[1].get("amount_brl")
        ):
            # Two identical snapshots of a single charge can be returned for
            # an undeliverable order. There is no distinct transaction ID.
            payment_events = [
                event for event in payment_events if event is not captures[1]
            ]
            captures = captures[:1]
        if expected_total is not None and len(captures) >= 3:
            matching_group = next(
                (
                    group
                    for size in range(len(captures) - 1, 1, -1)
                    for group in combinations(captures, size)
                    if sum(
                        (_decimal(event.get("amount_brl")) or Decimal(0) for event in group),
                        Decimal(0),
                    ) == expected_total
                ),
                None,
            )
            if matching_group is not None:
                selected_ids = {id(event) for event in matching_group}
                excluded_capture_amounts.extend(
                    _decimal(event.get("amount_brl")) or Decimal(0)
                    for event in captures if id(event) not in selected_ids
                )
                payment_events = [
                    event for event in payment_events
                    if event not in captures or id(event) in selected_ids
                ]
        anchored_amounts = [
            _decimal(event.get("amount_brl"))
            for event in payment_events
            if str(event.get("event_type", "")).lower() in {"captured", "capture"}
        ]
        anchored_amounts = [amount for amount in anchored_amounts if amount is not None]
        # Preserve payment rows that reconcile with the anchored lifecycle.
        # This prevents an old related-order row from inflating the total.
        if anchored_amounts:
            remaining = anchored_amounts.copy()
            filtered_rows: list[dict[str, Any]] = []
            for row in payment_rows:
                amount = _decimal(row.get("payment_value"))
                if amount is not None and amount in remaining:
                    filtered_rows.append(row)
                    remaining.remove(amount)
            if filtered_rows:
                payment_rows = filtered_rows
    opened_at = _date(case.get("opened_at"))
    if purchase_at and opened_at:
        refund_events = [
            event
            for event in refund_events
            if (event_at := _date(event.get("event_at")))
            and not _is_after(purchase_at, event_at)
            and not _is_after(event_at, opened_at)
        ]
    if excluded_capture_amounts:
        selected_capture_amounts = {
            _decimal(event.get("amount_brl"))
            for event in payment_events
            if str(event.get("event_type", "")).lower() in {"captured", "capture"}
        }
        refund_events = [
            event for event in refund_events
            if not (
                _decimal(event.get("amount_brl")) in excluded_capture_amounts
                and _decimal(event.get("amount_brl")) not in selected_capture_amounts
            )
        ]
    payment_refs = _unique([row.get("payment_sequential") for row in payment_rows])
    confirmed_captures = [
        event
        for event in payment_events
        if str(event.get("event_type", "")).lower() in {"captured", "capture"}
        and str(event.get("status", "")).lower()
        in {"confirmed", "completed", "succeeded", "success"}
    ]
    captured = _amount(confirmed_captures, "amount_brl")
    # Event capture totals are authoritative. Base payment rows are a fallback
    # only when the event stream is absent, never added to lifecycle totals.
    if captured is None and payment_rows and not payment_events:
        captured = _amount(payment_rows, "payment_value")

    successful_refunds = [
        event
        for event in refund_events
        if str(event.get("event_type", "")).lower()
        in {"refund_completed", "refund_processed", "refunded"}
        or (
            str(event.get("event_type", "")).lower().startswith("refund_")
            and str(event.get("status", "")).lower()
            in {"completed", "confirmed", "succeeded", "success"}
        )
    ]
    refunded = _amount(successful_refunds, "amount_brl")
    refund_failed = any(str(event.get("status", "")).lower() == "failed" for event in refund_events)
    refund_pending = any(
        str(event.get("status", "")).lower()
        in {"pending", "processing", "requested", "in_progress"}
        for event in refund_events
    )
    refund_requested = any(
        str(event.get("event_type", "")).lower().startswith("refund_") for event in refund_events
    )
    if refunded is None and refund_result and "events" in refund:
        # An authoritative, successful empty/current timeline proves no refund
        # has completed. A failed tool call leaves this amount unknown (null).
        refunded = Decimal(0)

    # Check whether the order actually supports the asserted problem. The first
    # claim is a lead only; confirmed lifecycle facts take precedence.
    status = str(context_order.get("order_status", "")).lower()
    shipment_events = _rows(shipment.get("events"))
    raw_late_events = [
        event
        for event in shipment_events
        if str(event.get("event_type", "")).lower()
        in {"delivered_late", "delivery_late", "late_delivery"}
        and str(event.get("status", "")).lower() in {"confirmed", "resolved", "completed"}
    ]
    delivery_anchor = context_order.get("order_delivered_customer_date") or context_order.get(
        "order_delivered_carrier_date"
    )
    anchored_late_events = [
        event
        for event in raw_late_events
        if _within_days(event.get("event_at"), delivery_anchor, 30)
    ]
    late_events = anchored_late_events or (raw_late_events if delivery_anchor is None else [])

    # Identify duplicate charges from distinct successful capture events with
    # the same amount; multiple different captures can be a legitimate split.
    capture_amounts = [_decimal(event.get("amount_brl")) for event in confirmed_captures]
    normalized_capture_amounts = [value for value in capture_amounts if value is not None]
    # Reconcile captures against the order's unique item rows. Repeated amounts
    # alone are not proof: two equal split payments can be legitimate.
    duplicate_capture = (
        len(normalized_capture_amounts) > 1
        and captured is not None
        and expected_total is not None
        and captured > expected_total + Decimal("0.01")
    )
    split_payment = (
        len(normalized_capture_amounts) > 1
        and not duplicate_capture
        and captured is not None
        and expected_total is not None
        and abs(captured - expected_total) <= Decimal("0.01")
    )

    # Reconcile the selected payment snapshot, not the unrelated item price.
    base_paid = _amount(payment_rows, "payment_value")
    mismatch_event = any(
        str(event.get("event_type", "")).lower() == "reconciliation_mismatch"
        for event in payment_events
    )
    capture_mismatch = (
        mismatch_event
        or (
            captured is not None
            and base_paid is not None
            and abs(captured - base_paid) > Decimal("0.01")
        )
    )

    shipment_verdict = "insufficient_evidence"
    late_sellers: list[str] = []
    seller_delayed = False
    for row in items:
        limit = _date(row.get("shipping_limit_date"))
        handed = _date(context_order.get("order_delivered_carrier_date"))
        if limit and handed and _is_after(handed, limit):
            seller_delayed = True
            sid = row.get("seller_id")
            if isinstance(sid, str) and sid not in late_sellers:
                late_sellers.append(sid)
    if status in {"canceled", "unavailable"}:
        shipment_verdict = "insufficient_evidence"
    elif late_events:
        actors = {str(event.get("actor", "")).lower() for event in late_events}
        shipment_verdict = "seller_delay" if "seller" in actors else "logistics_delay"
    elif seller_delayed:
        shipment_verdict = "seller_delay"
    else:
        delivered = _date(context_order.get("order_delivered_customer_date"))
        estimated = _date(context_order.get("order_estimated_delivery_date"))
        if delivered and estimated:
            shipment_verdict = "logistics_delay" if _is_after(delivered, estimated) else "on_time"
    if shipment_verdict != "seller_delay":
        late_sellers = []

    inferred: str | None = None
    if status in {"canceled", "unavailable"} and captured is not None and captured > 0:
        inferred = "canceled_order_paid" if status == "canceled" else "unavailable_order_paid"
    elif refund_failed:
        inferred = "refund_failed"
    elif refund_pending or (refund_requested and not successful_refunds and not refund_failed):
        inferred = "refund_pending"
    elif duplicate_capture:
        inferred = "duplicate_charge"
    elif late_events and shipment_verdict == "logistics_delay":
        inferred = "late_delivery_logistics"
    elif (late_events or seller_delayed) and shipment_verdict == "seller_delay":
        inferred = "late_delivery_seller"
    elif capture_mismatch:
        inferred = "payment_mismatch"
    elif split_payment:
        inferred = "valid_split_payment"
    elif topic in {"late_delivery_logistics", "late_delivery_seller"} and shipment_verdict in {
        "logistics_delay",
        "seller_delay",
    }:
        inferred = topic
    elif topic in {"unsupported_claim", "valid_split_payment"} and len(confirmed_captures) > 1:
        inferred = "valid_split_payment"
    elif resolved_id and order_result and payment_result and shipment_result:
        inferred = "unsupported_claim"
    else:
        inferred = "insufficient_evidence"

    policy_data = policy_result.get("data", {}) if policy_result else {}
    if not isinstance(policy_data, dict):
        policy_data = {}
    policy_rules = policy_data.get("rules", {})
    if not isinstance(policy_rules, dict):
        policy_rules = {}
    rule = policy_rules.get(inferred, {}) if isinstance(inferred, str) else {}
    if not isinstance(rule, dict):
        rule = {}
    recommended = _decimal(rule.get("refund_brl"))
    if recommended is None:
        recommended = Decimal(0)
    recommended = max(Decimal(0), recommended)
    if refunded is not None:
        recommended = max(Decimal(0), recommended - refunded)

    if inferred in {"refund_pending"}:
        payment_verdict = "refund_pending"
    elif inferred == "refund_failed":
        payment_verdict = "refund_failed"
    elif refunded is not None and refunded > 0:
        payment_verdict = "refunded"
    elif duplicate_capture:
        payment_verdict = "duplicate_capture"
    elif capture_mismatch:
        payment_verdict = "capture_mismatch"
    elif captured is not None:
        payment_verdict = "reconciled"
    else:
        payment_verdict = "insufficient_evidence"

    primary_issue = (
        inferred
        if inferred
        in {
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
        else "insufficient_evidence"
    )
    case_status = rule.get("case_status")
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        case_status = "needs_investigation"
    confidence = 0.93 if primary_issue not in {"insufficient_evidence"} else 0.35
    if primary_issue == "unsupported_claim":
        confidence = 0.85

    claim_results: list[dict[str, Any]] = []
    all_refs = [value["evidence_ref"] for value in evidence.values()]
    for index, claim in enumerate(request.get("claims", [])):
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        claim_id = claim["claim_id"]
        claim_topic = claim.get("topic")
        if index == 0:
            verdict = (
                "supported"
                if claim_topic == primary_issue
                else (
                    "insufficient_evidence"
                    if primary_issue == "insufficient_evidence"
                    else "unsupported"
                )
            )
            claim_refs = all_refs
            claim_confidence = confidence
        elif claim_topic == "requested_full_refund":
            if recommended <= 0:
                verdict = "unsupported"
            elif captured is None:
                verdict = "insufficient_evidence"
            elif recommended >= captured:
                verdict = "supported"
            else:
                verdict = "partially_supported"
            claim_refs = all_refs
            claim_confidence = 0.9 if captured is not None else 0.4
        else:
            verdict = "insufficient_evidence"
            claim_refs = all_refs
            claim_confidence = 0.4
        claim_results.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": claim_confidence,
                "evidence_refs": claim_refs[:30],
            }
        )

    # Make conflicts explicit where the same stable entity appears with
    # incompatible authoritative/source snapshots.
    conflicts: list[dict[str, Any]] = []
    matching_history = [row for row in history if row.get("order_id") == resolved_id]
    history_dates = _unique([row.get("order_purchase_timestamp") for row in matching_history])
    order_date = order.get("order_purchase_timestamp") if isinstance(order, dict) else None
    if len(history_dates) > 1 or (order_date and history_dates and order_date not in history_dates):
        selected_source = (
            "get_order"
            if context_order.get("order_purchase_timestamp") == order_date
            else "get_customer_history"
        )
        conflicts.append(
            {
                "field": "order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": selected_source,
                "resolution_code": "matched_case_opened_at",
            }
        )
    dup_item_ids = {
        row.get("order_item_id") for row in item_rows if isinstance(row.get("order_item_id"), str)
    }
    for item_id in dup_item_ids:
        rows = [row for row in item_rows if row.get("order_item_id") == item_id]
        if len({row.get("shipping_limit_date") for row in rows}) > 1:
            conflicts.append(
                {
                    "field": "shipping_limit_date",
                    "sources": ["get_order_items", "get_shipment_summary"],
                    "selected_source": "get_order_items",
                    "resolution_code": "closest_to_order_purchase",
                }
            )
            break
    if len(conflicts) < 5 and payment_events and payment_rows:
        event_sum = _amount(confirmed_captures, "amount_brl")
        row_sum = _amount(payment_rows, "payment_value")
        if (
            event_sum is not None
            and row_sum is not None
            and abs(event_sum - row_sum) > Decimal("0.01")
        ):
            conflicts.append(
                {
                    "field": "payment_total_brl",
                    "sources": ["get_payment_timeline.events", "get_payment_timeline.payments"],
                    "selected_source": "get_payment_timeline.events",
                    "resolution_code": "prefer_confirmed_lifecycle_events",
                }
            )

    actions: list[str] = []
    recommended_action = rule.get("recommended_action")
    if isinstance(recommended_action, str) and recommended_action:
        actions.append(recommended_action)
    if primary_issue == "unsupported_claim":
        actions = ["document_no_action"]
    if primary_issue == "insufficient_evidence":
        actions = ["investigate_order_evidence"]

    responsible = rule.get("responsible_parties", [])
    if not isinstance(responsible, list):
        responsible = []
    responsible = [
        item
        for item in responsible
        if isinstance(item, dict)
        and item.get("party_type")
        in {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
        and (item.get("party_id") is None or isinstance(item.get("party_id"), str))
    ][:5]
    for party in responsible:
        if party["party_type"] == "seller" and seller_ids:
            party["party_id"] = seller_ids[0]
    if not responsible:
        responsible = [{"party_type": "unknown", "party_id": None}]
    causes = (
        []
        if primary_issue in {"unsupported_claim", "insufficient_evidence"}
        else [
            {
                "cause_code": primary_issue.upper(),
                "rank": 1,
            }
        ]
    )

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=str(recommended_action or "no_policy_action"),
        evidence_refs=([policy_result["evidence_ref"]] if policy_result else []),
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="investigation-coordinator",
        target="verifier-agent",
        decision_code="cross_domain_verification",
        evidence_refs=all_refs[:20],
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code=primary_issue,
        evidence_refs=all_refs[:20],
        attributes={"evidence_count": len(all_refs), "resolved_order": bool(resolved_id)},
    )

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_results,
        "entity_resolution": {
            "status": "resolved" if resolved_id else ("not_found" if claimed_id else "ambiguous"),
            "resolved_order_ids": order_ids,
            "rejected_candidates": [
                candidate for candidate in candidates if candidate != resolved_id
            ][:20],
            "confidence": 0.98 if resolved_id else 0.25,
        },
        "customer_context": {
            "customer_unique_id": customer.get("customer_unique_id"),
            "related_order_ids": related_ids[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers[:20],
            "timeline_complete": bool(shipment_result and "events" in shipment),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": _money(captured),
            "refunded_total_brl": _money(refunded),
            "refundable_total_brl": _money(max(Decimal(0), captured - (refunded or Decimal(0))))
            if captured is not None
            else None,
        },
        "root_cause_analysis": {
            "ranked_causes": causes,
            "responsible_parties": responsible,
        },
        "evidence_refs": all_refs[:30],
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money(recommended) or 0.0,
            "refund_lines": (
                [
                    {
                        "reason_code": str(recommended_action or primary_issue),
                        "amount_brl": _money(recommended) or 0.0,
                        "entity_id": responsible[0].get("party_id"),
                    }
                ]
                if recommended > 0
                else []
            ),
        },
        "resolution_actions": _unique(actions)[:8],
    }
    return output
