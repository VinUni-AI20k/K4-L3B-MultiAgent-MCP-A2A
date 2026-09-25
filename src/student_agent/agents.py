from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)


@dataclass
class InvestigationContext:
    case: dict[str, Any]
    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter

    # Cache for MCP calls within the case scope (prevents duplicate tool calls)
    cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    all_evidence_refs: list[str] = field(default_factory=list)

    async def call_tool_cached(
        self, tool_name: str, actor: str, **arguments: str
    ) -> dict[str, Any] | None:
        """Call an MCP tool with caching and emit a tool_result_consumed trace event."""
        cache_key = f"{tool_name}:{sorted(arguments.items())}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            evidence = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
        except Exception as exc:
            logger.debug("Tool call %s failed for case %s: %s", tool_name, self.case_id, exc)
            return None

        self.cache[cache_key] = evidence
        ref = evidence.get("evidence_ref")
        if ref and ref not in self.all_evidence_refs:
            self.all_evidence_refs.append(ref)

        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref] if ref else [],
            attributes={"status": "success"},
        )
        return evidence


class EntityAgent:
    """Agent responsible for Entity Resolution and Customer Context."""

    def __init__(self, ctx: InvestigationContext) -> None:
        self.ctx = ctx
        self.actor = "entity-agent"

    async def run(self) -> dict[str, Any]:
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.actor,
            attributes={"task": "entity_resolution"},
        )

        case = self.ctx.case
        candidates = list(case.get("candidate_order_ids", []))
        claimed = case.get("customer_request", {}).get("claimed_order_id")
        if claimed and claimed not in candidates:
            candidates.insert(0, claimed)

        resolved_order_ids: list[str] = []
        rejected_candidates: list[str] = []

        # Validate candidate orders via get_order
        for candidate in candidates:
            evidence = await self.ctx.call_tool_cached(
                "get_order", self.actor, order_id=candidate
            )
            if evidence and evidence.get("data"):
                if candidate not in resolved_order_ids:
                    resolved_order_ids.append(candidate)
            else:
                if candidate not in rejected_candidates:
                    rejected_candidates.append(candidate)

        # Investigate customer context
        customer_hint = case.get("customer_unique_id_hint")
        related_order_ids: list[str] = []
        cust_unique_id: str | None = customer_hint

        scope = case.get("investigation_scope", {})
        if customer_hint and scope.get("include_customer_history", False):
            cust_evidence = await self.ctx.call_tool_cached(
                "get_customer_history", self.actor, customer_unique_id=customer_hint
            )
            if cust_evidence and cust_evidence.get("data"):
                data = cust_evidence["data"]
                orders = data.get("orders", [])
                for ord_item in orders:
                    oid = ord_item.get("order_id")
                    if oid and oid not in related_order_ids:
                        related_order_ids.append(oid)
                    if oid in candidates and oid not in resolved_order_ids:
                        resolved_order_ids.append(oid)
                        if oid in rejected_candidates:
                            rejected_candidates.remove(oid)

        # Deduce status
        if resolved_order_ids:
            status = "resolved"
            confidence = 0.95 if len(resolved_order_ids) == 1 else 0.85
        elif candidates:
            status = "not_found"
            confidence = 0.90
        else:
            status = "ambiguous"
            confidence = 0.50

        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="handoff",
            actor=self.actor,
            target="coordinator",
            attributes={"status": status, "resolved_count": len(resolved_order_ids)},
        )

        return {
            "entity_resolution": {
                "status": status,
                "resolved_order_ids": resolved_order_ids,
                "rejected_candidates": rejected_candidates,
                "confidence": confidence,
            },
            "customer_context": {
                "customer_unique_id": cust_unique_id,
                "related_order_ids": related_order_ids,
            },
        }


class OrderProductAgent:
    """Agent responsible for Order, Items, Product Context and Seller identification."""

    def __init__(self, ctx: InvestigationContext) -> None:
        self.ctx = ctx
        self.actor = "order-product-agent"

    async def run(self, resolved_order_ids: list[str]) -> dict[str, Any]:
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.actor,
            attributes={"task": "order_product_investigation"},
        )

        item_ids: list[str] = []
        seller_ids: list[str] = []
        scope = self.ctx.case.get("investigation_scope", {})

        for order_id in resolved_order_ids:
            # Order items
            items_evidence = await self.ctx.call_tool_cached(
                "get_order_items", self.actor, order_id=order_id
            )
            if items_evidence and items_evidence.get("data"):
                for itm in items_evidence["data"]:
                    iid = itm.get("order_item_id")
                    if iid and iid not in item_ids:
                        item_ids.append(iid)
                    sid = itm.get("seller_id")
                    if sid and sid not in seller_ids:
                        seller_ids.append(sid)

            # Sellers
            sellers_evidence = await self.ctx.call_tool_cached(
                "get_sellers", self.actor, order_id=order_id
            )
            if sellers_evidence and sellers_evidence.get("data"):
                for s in sellers_evidence["data"]:
                    sid = s.get("seller_id")
                    if sid and sid not in seller_ids:
                        seller_ids.append(sid)

            # Product context (if in scope)
            if scope.get("include_product_context", False):
                await self.ctx.call_tool_cached(
                    "get_product_context", self.actor, order_id=order_id
                )

        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="handoff",
            actor=self.actor,
            target="coordinator",
            attributes={"items_count": len(item_ids), "sellers_count": len(seller_ids)},
        )

        return {
            "item_ids": item_ids,
            "seller_ids": seller_ids,
        }


class ShipmentAgent:
    """Agent responsible for Shipment and Delivery Timeline analysis."""

    def __init__(self, ctx: InvestigationContext) -> None:
        self.ctx = ctx
        self.actor = "shipment-agent"

    async def run(self, resolved_order_ids: list[str]) -> dict[str, Any]:
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.actor,
            attributes={"task": "shipment_investigation"},
        )

        shipment_ids: list[str] = []
        late_seller_ids: list[str] = []
        timeline_complete = True
        verdict = "insufficient_evidence"

        for order_id in resolved_order_ids:
            summary = await self.ctx.call_tool_cached(
                "get_shipment_summary", self.actor, order_id=order_id
            )
            if not summary or not summary.get("data"):
                timeline_complete = False
                continue

            data = summary["data"]
            # Look at items for late shipping limits
            items = data.get("items", [])
            for itm in items:
                sid = itm.get("seller_id")
                limit_at = itm.get("shipping_limit_at")
                if sid:
                    # Collect shipment ID reference if available
                    shipment_ref = f"shipment-{order_id[:12]}"
                    if shipment_ref not in shipment_ids:
                        shipment_ids.append(shipment_ref)

            # Look at shipment events
            events = data.get("events", [])
            has_late_logistics = False
            has_late_seller = False
            has_delivered = False
            is_lost = False
            is_returned = False

            for ev in events:
                etype = ev.get("event_type", "")
                actor = ev.get("actor", "")
                if "late" in etype or "delay" in etype:
                    if actor == "logistics_provider" or "logistics" in etype:
                        has_late_logistics = True
                    elif actor == "seller" or "seller" in etype:
                        has_late_seller = True
                        sid = ev.get("seller_id")
                        if sid and sid not in late_seller_ids:
                            late_seller_ids.append(sid)
                elif "delivered" in etype:
                    has_delivered = True
                elif "lost" in etype:
                    is_lost = True
                elif "return" in etype:
                    is_returned = True

            # Also check get_order timestamps if available
            order_ev = self.ctx.cache.get(f"get_order:{sorted([('order_id', order_id)])}")
            if order_ev and order_ev.get("data"):
                odata = order_ev["data"]
                ostatus = odata.get("order_status")
                deliv_cust = odata.get("order_delivered_customer_date")
                est_deliv = odata.get("order_estimated_delivery_date")
                deliv_carrier = odata.get("order_delivered_carrier_date")

                if ostatus in ("canceled", "unavailable"):
                    verdict = "seller_delay"
                elif is_lost:
                    verdict = "lost"
                elif is_returned:
                    verdict = "returned"
                elif has_late_seller:
                    verdict = "seller_delay"
                elif has_late_logistics:
                    verdict = "logistics_delay"
                elif deliv_cust and est_deliv and deliv_cust > est_deliv:
                    verdict = "logistics_delay"
                elif deliv_cust:
                    verdict = "on_time"
                elif deliv_carrier:
                    verdict = "on_time"
                else:
                    verdict = "insufficient_evidence"
            else:
                if has_late_seller:
                    verdict = "seller_delay"
                elif has_late_logistics:
                    verdict = "logistics_delay"
                elif is_lost:
                    verdict = "lost"
                elif is_returned:
                    verdict = "returned"
                elif has_delivered:
                    verdict = "on_time"

        if not shipment_ids and resolved_order_ids:
            shipment_ids.append(f"shipment-{resolved_order_ids[0][:12]}")

        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="handoff",
            actor=self.actor,
            target="coordinator",
            attributes={"verdict": verdict, "timeline_complete": timeline_complete},
        )

        return {
            "shipment_analysis": {
                "verdict": verdict,
                "late_seller_ids": late_seller_ids,
                "timeline_complete": timeline_complete,
            },
            "shipment_ids": shipment_ids,
        }


class PaymentRefundAgent:
    """Agent responsible for Payment Reconciliations and Refund timelines."""

    def __init__(self, ctx: InvestigationContext) -> None:
        self.ctx = ctx
        self.actor = "payment-refund-agent"

    async def run(self, resolved_order_ids: list[str]) -> dict[str, Any]:
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.actor,
            attributes={"task": "payment_investigation"},
        )

        payment_references: list[str] = []
        captured_total = 0.0
        refunded_total = 0.0
        has_payments = False

        is_duplicate = False
        is_mismatch = False
        is_refund_pending = False
        is_refund_failed = False
        is_refunded = False

        for order_id in resolved_order_ids:
            # Payment timeline
            ptimeline = await self.ctx.call_tool_cached(
                "get_payment_timeline", self.actor, order_id=order_id
            )
            # Payments summary
            payments = await self.ctx.call_tool_cached(
                "get_order_payments", self.actor, order_id=order_id
            )
            # Refund timeline (optional)
            refund_tl = await self.ctx.call_tool_cached(
                "get_refund_timeline", self.actor, order_id=order_id
            )

            if payments and payments.get("data"):
                has_payments = True
                p_list = payments["data"]
                seen_payments = []
                for p in p_list:
                    pval = float(p.get("payment_value", 0.0))
                    captured_total += pval
                    pref = f"pay-{order_id[:8]}-{p.get('payment_sequential', '1')}"
                    if pref not in payment_references:
                        payment_references.append(pref)

                    # Check duplicate payment pattern
                    p_key = (p.get("payment_type"), pval)
                    if p_key in seen_payments and pval > 0:
                        is_duplicate = True
                    seen_payments.append(p_key)

            if ptimeline and ptimeline.get("data"):
                t_data = ptimeline["data"]
                events = t_data.get("events", [])
                for ev in events:
                    etype = ev.get("event_type", "")
                    if "duplicate" in etype:
                        is_duplicate = True
                    elif "mismatch" in etype:
                        is_mismatch = True

            if refund_tl and refund_tl.get("data"):
                r_data = refund_tl["data"]
                r_events = r_data.get("events", [])
                for rev in r_events:
                    ramount = float(rev.get("amount_brl", 0.0))
                    status = rev.get("status", "")
                    etype = rev.get("event_type", "")
                    if status == "confirmed" or etype == "refund_completed":
                        refunded_total += ramount
                        is_refunded = True
                    elif status == "pending" or etype == "refund_pending":
                        is_refund_pending = True
                    elif status == "failed" or etype == "refund_failed":
                        is_refund_failed = True

        captured_total = round(captured_total, 2)
        refunded_total = round(refunded_total, 2)
        refundable_total = round(max(0.0, captured_total - refunded_total), 2)

        if not has_payments:
            verdict = "insufficient_evidence"
            cap_field = None
            ref_field = None
            refable_field = None
        else:
            cap_field = captured_total
            ref_field = refunded_total
            refable_field = refundable_total

            if is_refund_failed:
                verdict = "refund_failed"
            elif is_refund_pending:
                verdict = "refund_pending"
            elif is_duplicate:
                verdict = "duplicate_capture"
            elif is_mismatch:
                verdict = "capture_mismatch"
            elif refunded_total >= captured_total and captured_total > 0:
                verdict = "refunded"
            else:
                verdict = "reconciled"

        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="handoff",
            actor=self.actor,
            target="coordinator",
            attributes={"verdict": verdict, "captured_total": cap_field},
        )

        return {
            "payment_analysis": {
                "verdict": verdict,
                "captured_total_brl": cap_field,
                "refunded_total_brl": ref_field,
                "refundable_total_brl": refable_field,
            },
            "payment_references": payment_references,
        }


class PolicyConflictAgent:
    """Agent responsible for Policy Adjudication, Data Conflicts, Root Causes and Financial Resolution."""

    def __init__(self, ctx: InvestigationContext) -> None:
        self.ctx = ctx
        self.actor = "policy-conflict-agent"

    async def run(
        self,
        entity_res: dict[str, Any],
        shipment_res: dict[str, Any],
        payment_res: dict[str, Any],
        seller_ids: list[str],
    ) -> dict[str, Any]:
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.actor,
            attributes={"task": "policy_and_conflict_resolution"},
        )

        case = self.ctx.case
        policy_ver = case.get("policy_version", "EC_POLICY_V2")
        policy_ev = await self.ctx.call_tool_cached(
            "get_policy", self.actor, policy_version=policy_ver
        )

        rules = {}
        if policy_ev and policy_ev.get("data"):
            rules = policy_ev["data"].get("rules", {})

        # Analyze order status from cached order
        resolved_orders = entity_res.get("resolved_order_ids", [])
        order_status = None
        if resolved_orders:
            order_ev = self.ctx.cache.get(f"get_order:{sorted([('order_id', resolved_orders[0])])}")
            if order_ev and order_ev.get("data"):
                order_status = order_ev["data"].get("order_status")

        ship_verdict = shipment_res["shipment_analysis"]["verdict"]
        pay_verdict = payment_res["payment_analysis"]["verdict"]
        refundable_total = payment_res["payment_analysis"]["refundable_total_brl"] or 0.0

        # Determine Primary Issue
        if not resolved_orders:
            primary_issue = "insufficient_evidence"
        elif order_status == "canceled":
            primary_issue = "canceled_order_paid"
        elif order_status == "unavailable":
            primary_issue = "unavailable_order_paid"
        elif pay_verdict == "refund_failed":
            primary_issue = "refund_failed"
        elif pay_verdict == "refund_pending":
            primary_issue = "refund_pending"
        elif pay_verdict == "duplicate_capture":
            primary_issue = "duplicate_charge"
        elif pay_verdict == "capture_mismatch":
            primary_issue = "payment_mismatch"
        elif ship_verdict == "seller_delay":
            primary_issue = "late_delivery_seller"
        elif ship_verdict == "logistics_delay":
            primary_issue = "late_delivery_logistics"
        elif pay_verdict == "reconciled" and ship_verdict == "on_time":
            # Check customer claims
            req = case.get("customer_request", {})
            claims = req.get("claims", [])
            topics = [c.get("topic") for c in claims]
            if "valid_split_payment" in topics:
                primary_issue = "valid_split_payment"
            elif any("refund" in t for t in topics) or any("late" in t for t in topics):
                primary_issue = "unsupported_claim"
            else:
                primary_issue = "unsupported_claim"
        else:
            primary_issue = "unsupported_claim"

        # Lookup rule
        rule = rules.get(primary_issue, {})
        case_status = rule.get("case_status", "no_action" if primary_issue in ("valid_split_payment", "unsupported_claim") else "action_required")
        rec_action = rule.get("recommended_action", "document_no_action")
        policy_refund_brl = float(rule.get("refund_brl", 0.0))
        responsible_parties = rule.get("responsible_parties", [])

        # Secondary issues
        secondary_issues: list[str] = []
        claims = case.get("customer_request", {}).get("claims", [])
        for c in claims:
            topic = c.get("topic")
            if topic and topic != primary_issue and topic not in secondary_issues:
                secondary_issues.append(topic[:80])

        # Claim assessments
        claim_assessments: list[dict[str, Any]] = []
        data_conflicts: list[dict[str, Any]] = []

        for c in claims:
            cid = c.get("claim_id", "claim-1")
            topic = c.get("topic", "")
            if topic == primary_issue:
                c_verdict = "supported"
                c_conf = 0.95
            elif topic == "requested_full_refund":
                if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
                    c_verdict = "supported"
                    c_conf = 0.90
                elif primary_issue in ("late_delivery_logistics", "late_delivery_seller"):
                    c_verdict = "partially_supported"
                    c_conf = 0.85
                    data_conflicts.append({
                        "field": "refund_amount",
                        "sources": ["customer_statement", "policy_guidelines"],
                        "selected_source": "policy_guidelines",
                        "resolution_code": "freight_refund_only_per_policy",
                    })
                else:
                    c_verdict = "unsupported"
                    c_conf = 0.90
                    data_conflicts.append({
                        "field": "refund_eligibility",
                        "sources": ["customer_statement", "authoritative_timeline"],
                        "selected_source": "authoritative_timeline",
                        "resolution_code": "on_time_delivery_verified",
                    })
            elif "late" in topic and ship_verdict == "on_time":
                c_verdict = "unsupported"
                c_conf = 0.95
                data_conflicts.append({
                    "field": "delivery_timeliness",
                    "sources": ["customer_statement", "carrier_logistics_timeline"],
                    "selected_source": "carrier_logistics_timeline",
                    "resolution_code": "verified_delivery_within_estimate",
                })
            else:
                c_verdict = "unsupported" if case_status == "no_action" else "partially_supported"
                c_conf = 0.80

            claim_assessments.append({
                "claim_id": cid,
                "verdict": c_verdict,
                "confidence": c_conf,
                "evidence_refs": [ref for ref in self.ctx.all_evidence_refs[:5]],
            })

        # Financial resolution
        if case_status == "no_action":
            final_refund = 0.0
            refund_lines = []
        else:
            final_refund = min(policy_refund_brl, refundable_total) if refundable_total > 0 else policy_refund_brl
            final_refund = round(final_refund, 2)
            party_id = seller_ids[0] if seller_ids else None
            refund_lines = [{
                "reason_code": primary_issue,
                "amount_brl": final_refund,
                "entity_id": party_id,
            }] if final_refund > 0 else []

        # Resolution actions
        actions = [rec_action]
        if case_status == "action_required" and "notify_customer" not in actions:
            actions.append("notify_customer")

        # Root cause
        ranked_causes = [{"cause_code": primary_issue.upper(), "rank": 1}]
        if not responsible_parties:
            responsible_parties = [{"party_type": "platform", "party_id": None}]

        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="policy_decided",
            actor=self.actor,
            decision_code=primary_issue,
            attributes={"case_status": case_status, "refund_brl": final_refund},
        )

        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="handoff",
            actor=self.actor,
            target="verifier",
            attributes={"primary_issue": primary_issue},
        )

        return {
            "assessment": {
                "primary_issue": primary_issue,
                "secondary_issues": secondary_issues[:10],
                "case_status": case_status,
                "confidence": 0.92,
            },
            "claim_assessments": claim_assessments,
            "data_conflicts": data_conflicts[:5],
            "root_cause_analysis": {
                "ranked_causes": ranked_causes,
                "responsible_parties": responsible_parties[:5],
            },
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": final_refund,
                "refund_lines": refund_lines,
            },
            "resolution_actions": actions[:8],
        }
