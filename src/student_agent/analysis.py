"""Deterministic, case-scoped interpretation of MCP evidence."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def rows(value: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in keys:
            child = value.get(key)
            if isinstance(child, list):
                return [item for item in child if isinstance(item, dict)]
    return []


def at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def number(value: Decimal | None) -> float | None:
    return float(value.quantize(Decimal("0.01"))) if value is not None else None


def purchase_times(order: dict[str, Any], history: dict[str, Any]) -> list[datetime]:
    values = [at(order.get("order_purchase_timestamp"))]
    values += [at(row.get("order_purchase_timestamp")) for row in rows(history, "orders")]
    return sorted(set(value for value in values if value is not None))


def belongs_to_purchase(value: Any, anchor: datetime | None, purchases: list[datetime]) -> bool:
    """Assign each dated row to its nearest preceding purchase episode.

    The gateway can return several orders with the same order ID.  An order ID
    alone cannot scope payment, refund, item or shipment rows in those cases.
    """
    event_at = at(value)
    if event_at is None or anchor is None:
        return False
    preceding = [purchase for purchase in purchases if purchase <= event_at]
    if not preceding:
        return False
    return max(preceding) == anchor


def scoped_rows(
    values: list[dict[str, Any]],
    date_key: str,
    anchor: datetime | None,
    purchases: list[datetime],
) -> list[dict[str, Any]]:
    return [row for row in values if belongs_to_purchase(row.get(date_key), anchor, purchases)]


def analyze_items(data: Any, anchor: datetime | None, purchases: list[datetime]) -> dict[str, Any]:
    selected = scoped_rows(rows(data), "shipping_limit_date", anchor, purchases)
    total = Decimal("0")
    valid_total = bool(selected)
    for item in selected:
        price, freight = money(item.get("price")), money(item.get("freight_value"))
        if price is None or freight is None:
            valid_total = False
        else:
            total += price + freight
    return {
        "rows": selected,
        "item_ids": sorted(
            {item["order_item_id"] for item in selected if item.get("order_item_id")}
        ),
        "seller_ids": sorted({item["seller_id"] for item in selected if item.get("seller_id")}),
        "total": number(total) if valid_total else None,
    }


def analyze_payment(
    payment_data: Any,
    timeline_data: Any,
    refund_data: Any,
    anchor: datetime | None,
    purchases: list[datetime],
    item_total: float | None,
) -> dict[str, Any]:
    timeline = timeline_data if isinstance(timeline_data, dict) else {}
    refund = refund_data if isinstance(refund_data, dict) else {}
    events = scoped_rows(rows(timeline, "events"), "event_at", anchor, purchases)
    refund_events = scoped_rows(rows(refund, "events"), "event_at", anchor, purchases)
    captures = [
        event
        for event in events
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"
    ]
    capture_amounts = [money(event.get("amount_brl")) for event in captures]
    captured = sum((value for value in capture_amounts if value is not None), Decimal("0"))
    confirmed_refunds = [
        event
        for event in refund_events
        if event.get("status") in {"confirmed", "completed", "succeeded", "refunded"}
    ]
    refunded = sum(
        (money(event.get("amount_brl")) or Decimal("0") for event in confirmed_refunds),
        Decimal("0"),
    )
    latest_refund = max(
        refund_events,
        key=lambda event: at(event.get("event_at")) or anchor,
        default=None,
    )
    mismatch_event = any(
        event.get("event_type") == "reconciliation_mismatch"
        and event.get("status") in {"open", "confirmed"}
        for event in events
    )
    expected = money(item_total)
    duplicate = (
        len(captures) > 1
        and expected is not None
        and captured > expected
        and len({value for value in capture_amounts if value is not None}) == 1
    )
    if latest_refund and latest_refund.get("status") == "failed":
        verdict = "refund_failed"
    elif latest_refund and latest_refund.get("status") in {"pending", "requested"}:
        verdict = "refund_pending"
    elif refunded > 0:
        verdict = "refunded"
    elif duplicate:
        verdict = "duplicate_capture"
    elif mismatch_event or captures and expected is not None and captured != expected:
        verdict = "capture_mismatch"
    elif captures:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"

    # Match undated base rows to the dated captures.  This avoids attaching
    # payment references from another purchase episode to this case.
    candidates = rows(payment_data) or rows(timeline, "payments")
    remaining = Counter(value for value in capture_amounts if value is not None)
    selected_payments = []
    for row in candidates:
        amount = money(row.get("payment_value"))
        if amount is not None and remaining[amount] > 0:
            selected_payments.append(row)
            remaining[amount] -= 1
    references = {
        str(row[key])
        for row in selected_payments
        for key in ("payment_id", "payment_reference", "transaction_id")
        if row.get(key)
    }
    return {
        "verdict": verdict,
        "captured": number(captured) if captures else None,
        "refunded": number(refunded) if refund_events else 0.0,
        "refundable": number(max(Decimal("0"), captured - refunded)) if captures else None,
        "payment_references": sorted(references),
        "events": events,
        "refund_events": refund_events,
        "selected_payments": selected_payments,
        "mismatch_event": mismatch_event,
        "duplicate": duplicate,
    }


def analyze_shipment(
    data: Any, seller_data: Any, anchor: datetime | None, purchases: list[datetime]
) -> dict[str, Any]:
    shipment = data if isinstance(data, dict) else {}
    events = scoped_rows(rows(shipment, "events"), "event_at", anchor, purchases)
    limits = scoped_rows(rows(shipment, "shipping_limits"), "shipping_limit_at", anchor, purchases)
    carrier = at(shipment.get("delivered_carrier_at"))
    delivered = at(shipment.get("delivered_customer_at"))
    estimated = at(shipment.get("estimated_delivery_at"))
    late_sellers = {
        str(row["seller_id"])
        for row in limits
        if row.get("seller_id")
        and carrier
        and at(row.get("shipping_limit_at"))
        and carrier > at(row.get("shipping_limit_at"))
    }
    confirmed = [event for event in events if event.get("status") == "confirmed"]
    seller_event = any(event.get("actor") == "seller" for event in confirmed)
    logistics_event = any(event.get("actor") == "logistics_provider" for event in confirmed)
    types = {str(event.get("event_type")) for event in confirmed}
    if "lost" in types:
        verdict = "lost"
    elif "returned" in types:
        verdict = "returned"
    elif seller_event or late_sellers:
        verdict = "seller_delay"
    elif logistics_event or (delivered and estimated and delivered > estimated):
        verdict = "logistics_delay"
    elif delivered and estimated:
        verdict = "on_time"
    else:
        verdict = "insufficient_evidence"
    sellers = {str(row["seller_id"]) for row in limits if row.get("seller_id")}
    sellers.update(str(row["seller_id"]) for row in rows(seller_data) if row.get("seller_id"))
    shipment_ids = {
        str(row[key])
        for row in [shipment, *events]
        for key in ("shipment_id", "tracking_id")
        if row.get(key)
    }
    return {
        "verdict": verdict,
        "late_seller_ids": sorted(late_sellers),
        "seller_ids": sorted(sellers),
        "shipment_ids": sorted(shipment_ids),
        "timeline_complete": bool(carrier and delivered and estimated),
        "events": events,
        "limits": limits,
        "order_status": shipment.get("order_status"),
    }
