"""Payment and Financial Specialist Agent."""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .models import CaseEvidenceContext, PaymentFindings


class PaymentAgent:
    """Specialist responsible for payment captures, installments, reconciliation and refunds."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self, case_id: str, order_id: str, context: CaseEvidenceContext
    ) -> PaymentFindings:
        evidence_refs: list[str] = []
        findings = PaymentFindings(order_id=order_id, verdict="reconciled")

        # 1. get_order_payments
        payments_data: list[dict[str, Any]] = []
        try:
            cached_payments = context.get_cached("get_order_payments", {"order_id": order_id})
            if cached_payments is None:
                pay_ev = await self.gateway.call(
                    "get_order_payments", case_id=case_id, order_id=order_id
                )
                context.set_cached("get_order_payments", {"order_id": order_id}, pay_ev)
            else:
                pay_ev = cached_payments

            ref = pay_ev["evidence_ref"]
            evidence_refs.append(ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_order_payments",
                evidence_refs=[ref],
                attributes={"payment_rows": len(pay_ev["data"])},
            )
            payments_data = pay_ev["data"]
            findings.payments = payments_data
            for idx, p in enumerate(payments_data):
                val = float(p.get("payment_value", 0.0))
                findings.captured_total_brl += val
                seq = p.get("payment_sequential", idx + 1)
                findings.payment_references.append(f"pay_{order_id[:8]}_{seq}_{idx + 1}")
        except Exception:
            pass

        # 2. get_payment_timeline
        timeline_events: list[dict[str, Any]] = []
        try:
            cached_pt = context.get_cached("get_payment_timeline", {"order_id": order_id})
            if cached_pt is None:
                pt_ev = await self.gateway.call(
                    "get_payment_timeline", case_id=case_id, order_id=order_id
                )
                context.set_cached("get_payment_timeline", {"order_id": order_id}, pt_ev)
            else:
                pt_ev = cached_pt

            ref = pt_ev["evidence_ref"]
            evidence_refs.append(ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_payment_timeline",
                evidence_refs=[ref],
                attributes={"events_count": len(pt_ev["data"].get("events", []))},
            )
            timeline_events = pt_ev["data"].get("events", [])
            findings.timeline_events = timeline_events

            for ev in timeline_events:
                ev_type = ev.get("event_type")
                status = ev.get("status")
                if ev_type == "reconciliation_mismatch" and status == "open":
                    findings.has_mismatch = True

            # Detect duplicate captures in events
            captured_events = [ev for ev in timeline_events if ev.get("event_type") == "captured"]
            if len(captured_events) >= 2:
                amounts = [ev.get("amount_brl") for ev in captured_events]
                has_dup_amt = len(amounts) != len(set(amounts))
                more_events_than_rows = len(captured_events) > len(payments_data)
                four_identical = len(amounts) >= 4 and len(set(amounts)) == 1
                if (has_dup_amt and more_events_than_rows) or four_identical:
                    findings.has_duplicate_capture = True

        except Exception:
            pass

        # 3. get_refund_timeline
        refund_events: list[dict[str, Any]] = []
        try:
            cached_rt = context.get_cached("get_refund_timeline", {"order_id": order_id})
            if cached_rt is None:
                rt_ev = await self.gateway.call(
                    "get_refund_timeline", case_id=case_id, order_id=order_id
                )
                context.set_cached("get_refund_timeline", {"order_id": order_id}, rt_ev)
            else:
                rt_ev = cached_rt

            ref = rt_ev["evidence_ref"]
            evidence_refs.append(ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_refund_timeline",
                evidence_refs=[ref],
                attributes={"refund_events": len(rt_ev["data"].get("events", []))},
            )
            refund_events = rt_ev["data"].get("events", [])
            findings.refund_events = refund_events

            for rev in refund_events:
                status = rev.get("status")
                amt = float(rev.get("amount_brl", 0.0))
                if status == "pending":
                    findings.has_pending_refund = True
                elif status == "failed":
                    findings.has_failed_refund = True
                elif status in ("completed", "processed", "confirmed"):
                    findings.refunded_total_brl += amt

        except Exception:
            pass

        remaining = findings.captured_total_brl - findings.refunded_total_brl
        findings.refundable_total_brl = max(0.0, round(remaining, 2))
        findings.captured_total_brl = round(findings.captured_total_brl, 2)
        findings.refunded_total_brl = round(findings.refunded_total_brl, 2)

        # Determine payment verdict
        if findings.has_duplicate_capture:
            findings.verdict = "duplicate_capture"
        elif findings.has_mismatch:
            findings.verdict = "capture_mismatch"
        elif findings.has_failed_refund:
            findings.verdict = "refund_failed"
        elif findings.has_pending_refund:
            findings.verdict = "refund_pending"
        elif findings.refunded_total_brl > 0:
            findings.verdict = "refunded"
        else:
            findings.verdict = "reconciled"

        findings.evidence_refs = evidence_refs
        return findings
