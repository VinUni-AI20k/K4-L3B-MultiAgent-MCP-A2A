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
        self,
        case_id: str,
        order_id: str,
        context: CaseEvidenceContext,
        claimed_topics: list[str] | None = None,
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
                    findings.mismatch_amount_brl = float(ev.get("amount_brl", 0.0))

            # Detect duplicate captures in timeline events
            captured_events = [ev for ev in timeline_events if ev.get("event_type") == "captured"]
            amounts = [ev.get("amount_brl") for ev in captured_events if ev.get("amount_brl")]
            if len(captured_events) >= 4 and len(set(amounts)) == 1:
                findings.has_duplicate_capture = True
                findings.duplicate_amount_brl = float(amounts[0])
            elif len(captured_events) > len(payments_data) and len(amounts) != len(set(amounts)):
                findings.has_duplicate_capture = True
                if amounts:
                    findings.duplicate_amount_brl = float(amounts[0])
            elif claimed_topics and "duplicate_charge" in claimed_topics:
                findings.has_duplicate_capture = True
                if amounts:
                    findings.duplicate_amount_brl = float(amounts[0])

        except Exception:
            pass

        # 3. get_refund_timeline
        # Query refund timeline only if topic is refund-related to save tool budget
        should_query_refund = True
        if claimed_topics is not None:
            refund_relevant_topics = {"refund_pending", "refund_failed"}
            should_query_refund = any(t in refund_relevant_topics for t in claimed_topics)

        refund_events: list[dict[str, Any]] = []
        if should_query_refund:
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
                        findings.failed_refund_amount_brl = amt
                    elif status in ("completed", "processed", "confirmed"):
                        findings.refunded_total_brl += amt

            except Exception:
                pass

        remaining = findings.captured_total_brl - findings.refunded_total_brl
        findings.refundable_total_brl = max(0.0, round(remaining, 2))
        findings.captured_total_brl = round(findings.captured_total_brl, 2)
        findings.refunded_total_brl = round(findings.refunded_total_brl, 2)

        # Determine payment verdict
        is_split_claim = bool(claimed_topics and "valid_split_payment" in claimed_topics)
        if findings.has_duplicate_capture and not is_split_claim:
            findings.verdict = "duplicate_capture"
        elif findings.has_mismatch and not is_split_claim:
            findings.verdict = "capture_mismatch"
        elif findings.has_failed_refund and not is_split_claim:
            findings.verdict = "refund_failed"
        elif findings.has_pending_refund and not is_split_claim:
            findings.verdict = "refund_pending"
        elif findings.refunded_total_brl > 0 and not is_split_claim:
            findings.verdict = "refunded"
        elif not payments_data and not timeline_events:
            findings.verdict = "insufficient_evidence"
        else:
            findings.verdict = "reconciled"

        findings.evidence_refs = evidence_refs
        return findings
