"""Small, evidence-only specialist agents used by the L3B coordinator.

The agents deliberately return plain handoff dictionaries.  That makes their
inputs/outputs inspectable, keeps tool ownership narrow, and prevents a
specialist from inventing facts when an MCP domain is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("rows", "payments", "events", "refunds", "orders", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        return [value]
    return []


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _amount(value: Any) -> float | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(amount) if amount >= 0 else None


def _total(rows: list[dict[str, Any]], *keys: str) -> float | None:
    values = [_amount(row.get(key)) for row in rows for key in keys if key in row]
    parsed = [value for value in values if value is not None]
    return round(sum(parsed), 2) if parsed else None


def _at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Handoff:
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)

    @property
    def refs(self) -> list[str]:
        return [item["evidence_ref"] for item in self.evidence.values()]


class Specialist:
    actor = "specialist"

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter, case_id: str) -> None:
        self.gateway, self.trace, self.case_id = gateway, trace, case_id

    async def _call(self, handoff: Handoff, tool: str, **arguments: str) -> None:
        try:
            evidence = await self.gateway.call(tool, case_id=self.case_id, **arguments)
        except (RuntimeError, ValueError):
            handoff.unavailable.add(tool)
            return
        handoff.evidence[tool] = evidence
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=self.actor,
            tool_name=tool,
            evidence_refs=[evidence["evidence_ref"]],
        )


class OrderAgent(Specialist):
    actor = "order-agent"

    async def investigate(self, case: dict[str, Any]) -> tuple[Handoff, dict[str, Any]]:
        handoff = Handoff()
        request = case.get("customer_request", {})
        order_id = _text(request.get("claimed_order_id"))
        if not order_id:
            return handoff, {"resolved": None, "rejected": []}
        await self._call(handoff, "get_order", order_id=order_id)
        order = handoff.evidence.get("get_order", {}).get("data")
        if not isinstance(order, dict) or _text(order.get("order_id")) != order_id:
            return handoff, {
                "resolved": None,
                "rejected": list(case.get("candidate_order_ids", [])),
            }
        await self._call(handoff, "get_order_items", order_id=order_id)
        if case.get("investigation_scope", {}).get("include_customer_history"):
            hint = _text(case.get("customer_unique_id_hint"))
            if hint:
                await self._call(handoff, "get_customer_history", customer_unique_id=hint)
        if case.get("investigation_scope", {}).get("include_product_context"):
            await self._call(handoff, "get_product_context", order_id=order_id)
        rejected = [
            candidate for candidate in case.get("candidate_order_ids", []) if candidate != order_id
        ]
        return handoff, {"resolved": order_id, "rejected": rejected, "order": order}


class PaymentAgent(Specialist):
    actor = "payment-agent"

    async def investigate(self, order_id: str) -> tuple[Handoff, dict[str, Any]]:
        handoff = Handoff()
        for tool in ("get_order_payments", "get_payment_timeline", "get_refund_timeline"):
            await self._call(handoff, tool, order_id=order_id)
        payments = _rows(handoff.evidence.get("get_order_payments", {}).get("data"))
        timeline = _rows(handoff.evidence.get("get_payment_timeline", {}).get("data"))
        refunds = _rows(handoff.evidence.get("get_refund_timeline", {}).get("data"))
        captured = _total(payments, "payment_value", "amount_brl", "amount")
        # Lifecycle rows can contain payment references and statuses but should
        # not be double-counted against the base payment rows.
        event_text = " ".join(str(row).lower() for row in [*timeline, *refunds])
        refunded = _total(
            refunds, "refunded_amount_brl", "refund_amount_brl", "amount_brl", "amount"
        )
        if "refund_failed" in event_text or "failed" in event_text and "refund" in event_text:
            verdict = "refund_failed"
        elif "refund_pending" in event_text or "pending" in event_text and "refund" in event_text:
            verdict = "refund_pending"
        elif refunded and refunded > 0:
            verdict = "refunded"
        elif "duplicate" in event_text:
            verdict = "duplicate_capture"
        elif "mismatch" in event_text:
            verdict = "capture_mismatch"
        elif payments:
            verdict = "reconciled"
        else:
            verdict = "insufficient_evidence"
        refs = []
        for row in [*payments, *timeline]:
            for key in ("payment_id", "payment_reference", "transaction_id"):
                if _text(row.get(key)):
                    refs.append(row[key])
        return handoff, {
            "verdict": verdict,
            "captured": captured,
            "refunded": refunded or 0.0,
            "payment_references": sorted(set(refs)),
        }


class ShipmentSellerAgent(Specialist):
    actor = "shipment-seller-agent"

    async def investigate(self, order_id: str) -> tuple[Handoff, dict[str, Any]]:
        handoff = Handoff()
        await self._call(handoff, "get_shipment_summary", order_id=order_id)
        await self._call(handoff, "get_sellers", order_id=order_id)
        shipment = handoff.evidence.get("get_shipment_summary", {}).get("data")
        shipment = shipment if isinstance(shipment, dict) else {}
        events = _rows(shipment.get("events"))
        limits = _rows(shipment.get("shipping_limits"))
        sellers = {row.get("seller_id") for row in limits if _text(row.get("seller_id"))}
        sellers.update(
            row.get("seller_id")
            for row in _rows(handoff.evidence.get("get_sellers", {}).get("data"))
            if _text(row.get("seller_id"))
        )
        event_text = " ".join(str(row).lower() for row in events)
        delivered = _at(shipment.get("delivered_customer_at"))
        estimated = _at(shipment.get("estimated_delivery_at"))
        carrier = _at(shipment.get("delivered_carrier_at"))
        # The same seller may have several item rows.  A seller handoff is
        # late only when it misses that seller's final contractual deadline;
        # treating an earlier duplicate row as decisive causes false blame.
        seller_limits: dict[str, datetime] = {}
        for row in limits:
            seller_id, limit_at = _text(row.get("seller_id")), _at(row.get("shipping_limit_at"))
            if (
                seller_id
                and limit_at
                and (seller_id not in seller_limits or limit_at > seller_limits[seller_id])
            ):
                seller_limits[seller_id] = limit_at
        late_sellers = {
            seller_id
            for seller_id, limit_at in seller_limits.items()
            if carrier and carrier > limit_at
        }
        if "lost" in event_text:
            verdict = "lost"
        elif "returned" in event_text:
            verdict = "returned"
        elif "seller_delay" in event_text:
            verdict = "seller_delay"
        elif "logistics" in event_text or "delivered_late" in event_text:
            verdict = "logistics_delay"
        elif late_sellers:
            verdict = "seller_delay"
        elif delivered and estimated and delivered > estimated:
            verdict = "logistics_delay"
        elif delivered:
            verdict = "on_time"
        else:
            verdict = "insufficient_evidence"
        return handoff, {
            "verdict": verdict,
            "late_seller_ids": sorted(late_sellers),
            "seller_ids": sorted(value for value in sellers if isinstance(value, str)),
            "shipment_ids": sorted(
                {
                    row[key]
                    for row in [shipment, *events]
                    for key in ("shipment_id", "tracking_id")
                    if _text(row.get(key))
                }
            ),
            "timeline_complete": bool(events or delivered),
            "shipment": shipment,
        }


class PolicyAgent(Specialist):
    actor = "policy-agent"

    async def investigate(self, policy_version: str) -> tuple[Handoff, dict[str, Any]]:
        handoff = Handoff()
        await self._call(handoff, "get_policy", policy_version=policy_version)
        return handoff, {"available": "get_policy" in handoff.evidence}
