"""Shipment and Logistics Specialist Agent."""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .models import CaseEvidenceContext, ShipmentFindings


class ShipmentAgent:
    """Specialist responsible for transit timeline, carrier milestones, and delivery delays."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self,
        case_id: str,
        order_id: str,
        context: CaseEvidenceContext,
        fallback_seller_ids: list[str] | None = None,
    ) -> ShipmentFindings:
        evidence_refs: list[str] = []
        findings = ShipmentFindings(order_id=order_id, verdict="on_time")

        try:
            cached_shipment = context.get_cached("get_shipment_summary", {"order_id": order_id})
            if cached_shipment is None:
                ship_ev = await self.gateway.call(
                    "get_shipment_summary", case_id=case_id, order_id=order_id
                )
                context.set_cached("get_shipment_summary", {"order_id": order_id}, ship_ev)
            else:
                ship_ev = cached_shipment

            ref = ship_ev["evidence_ref"]
            evidence_refs.append(ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="shipment_agent",
                tool_name="get_shipment_summary",
                evidence_refs=[ref],
                attributes={"status": ship_ev["data"].get("order_status")},
            )
            data: dict[str, Any] = ship_ev["data"]
            carrier_at = data.get("delivered_carrier_at")
            customer_at = data.get("delivered_customer_at")
            estimated_at = data.get("estimated_delivery_at")
            limits: list[dict[str, Any]] = data.get("shipping_limits") or []
            events: list[dict[str, Any]] = data.get("events") or []

            findings.delivered_carrier_at = carrier_at
            findings.delivered_customer_at = customer_at
            findings.estimated_delivery_at = estimated_at
            findings.shipping_limits = limits
            findings.timeline_complete = bool(carrier_at and customer_at and estimated_at)
            ship_id = data.get("shipment_id") or f"shipment-{order_id[:12]}"
            findings.shipment_ids = [ship_id]

            # 1. Inspect verified milestones from authoritative telemetry events
            seller_delay_event = False
            logistics_delay_event = False
            for ev in events:
                if ev.get("event_type") == "delivered_late" and ev.get("status") == "confirmed":
                    actor = ev.get("actor")
                    if actor == "seller":
                        seller_delay_event = True
                    elif actor == "logistics_provider":
                        logistics_delay_event = True

            # 2. Evaluate shipping limits and seller handoff
            late_sellers: list[str] = []
            for limit in limits:
                limit_at = limit.get("shipping_limit_at")
                seller_id = limit.get("seller_id")
                if (
                    carrier_at
                    and limit_at
                    and carrier_at > limit_at
                    and seller_id
                    and seller_id not in late_sellers
                ):
                    late_sellers.append(seller_id)

            if seller_delay_event and not late_sellers:
                for limit in limits:
                    sid = limit.get("seller_id")
                    if sid and sid not in late_sellers:
                        late_sellers.append(sid)
                if not late_sellers and fallback_seller_ids:
                    late_sellers.extend(fallback_seller_ids)

            # 3. Evaluate logistics carrier delivery vs estimated delivery date
            is_logistics_delay_time = bool(
                customer_at and estimated_at and customer_at > estimated_at
            )

            # 4. Final Verdict determination
            if seller_delay_event:
                findings.is_seller_delay = True
                findings.late_seller_ids = late_sellers
                findings.verdict = "seller_delay"
            elif logistics_delay_event:
                findings.is_logistics_delay = True
                findings.late_seller_ids = []
                findings.verdict = "logistics_delay"
            elif len(late_sellers) > 0:
                findings.is_seller_delay = True
                findings.late_seller_ids = late_sellers
                findings.verdict = "seller_delay"
            elif is_logistics_delay_time:
                findings.is_logistics_delay = True
                findings.late_seller_ids = []
                findings.verdict = "logistics_delay"
            elif customer_at:
                findings.verdict = "on_time"
            else:
                order_status = data.get("order_status")
                if order_status == "canceled":
                    findings.verdict = "on_time"
                elif order_status == "unavailable":
                    findings.verdict = "lost"
                else:
                    findings.verdict = "insufficient_evidence"

        except Exception:
            findings.verdict = "insufficient_evidence"
            findings.late_seller_ids = []

        findings.evidence_refs = evidence_refs
        return findings
