"""Case-scoped specialist agents and their observable evidence handoffs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .analysis import analyze_items, analyze_payment, analyze_shipment, at, purchase_times, rows
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ORDER_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass
class Handoff:
    facts: dict[str, Any] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)


class EvidenceBroker:
    """One-call-per-tool-and-arguments cache scoped to a single case."""

    def __init__(
        self, gateway: EvidenceGateway, trace: TraceWriter, case_id: str, tools: set[str]
    ) -> None:
        self.gateway, self.trace, self.case_id, self.tools = gateway, trace, case_id, tools
        self.results: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self.failures: set[str] = set()

    @staticmethod
    def _key(tool: str, arguments: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return tool, tuple(sorted(arguments.items()))

    async def call(self, actor: str, tool: str, **arguments: str) -> dict[str, Any] | None:
        key = self._key(tool, arguments)
        if key in self.results:
            return self.results[key]
        if tool not in self.tools:
            self.failures.add(tool)
            return None
        try:
            result = await self.gateway.call(tool, case_id=self.case_id, **arguments)
        except (RuntimeError, ValueError):
            self.failures.add(tool)
            return None
        self.results[key] = result
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[result["evidence_ref"]],
        )
        return result

    def ref(self, tool: str, **arguments: str) -> str | None:
        result = self.results.get(self._key(tool, arguments))
        return result["evidence_ref"] if result else None

    def refs(self, *tools: str) -> list[str]:
        selected = set(tools)
        return list(
            dict.fromkeys(
                result["evidence_ref"]
                for (tool, _), result in self.results.items()
                if tool in selected
            )
        )


class EntityAgent:
    actor = "entity-agent"

    def __init__(self, evidence: EvidenceBroker) -> None:
        self.evidence = evidence

    async def investigate(self, case: dict[str, Any]) -> Handoff:
        request = case.get("customer_request", {})
        claimed = request.get("claimed_order_id")
        candidates = list(
            dict.fromkeys(
                value
                for value in [claimed, *case.get("candidate_order_ids", [])]
                if isinstance(value, str) and value
            )
        )
        hint = case.get("customer_unique_id_hint")
        history_result = None
        if case.get("investigation_scope", {}).get("include_customer_history") and isinstance(
            hint, str
        ):
            history_result = await self.evidence.call(
                self.actor, "get_customer_history", customer_unique_id=hint
            )
        history = history_result.get("data", {}) if history_result else {}
        history_orders = rows(history, "orders")
        scored: list[tuple[int, str, dict[str, Any]]] = []
        rejected: list[str] = []
        for candidate in candidates:
            if not ORDER_ID.fullmatch(candidate):
                rejected.append(candidate)
                continue
            response = await self.evidence.call(self.actor, "get_order", order_id=candidate)
            order = response.get("data") if response else None
            if not isinstance(order, dict) or order.get("order_id") != candidate:
                rejected.append(candidate)
                continue
            matching = [
                row
                for row in history_orders
                if row.get("order_id") == candidate
                and row.get("order_purchase_timestamp") == order.get("order_purchase_timestamp")
                and row.get("customer_id") == order.get("customer_id")
            ]
            score = 1 + (3 if matching else 0) + (1 if candidate == claimed else 0)
            scored.append((score, candidate, order))
        scored.sort(key=lambda item: (-item[0], item[1]))
        top = scored[0] if scored else None
        ambiguous = bool(top and len(scored) > 1 and scored[1][0] == top[0])
        resolved = None if ambiguous or not top else top[1]
        rejected += [candidate for _, candidate, _ in scored if candidate != resolved]
        related = sorted(
            {
                str(row["order_id"])
                for row in history_orders
                if row.get("order_id") and row.get("order_id") != resolved
            }
        )
        facts = {
            "status": "ambiguous" if ambiguous else "resolved" if resolved else "not_found",
            "order_id": resolved,
            "order": top[2] if resolved and top else {},
            "rejected_candidates": sorted(set(rejected)),
            "customer_unique_id": history.get("customer_unique_id")
            if isinstance(history, dict)
            else None,
            "related_order_ids": related[:20],
            "history": history,
            "confidence": 0.98
            if top and top[0] >= 5 and not ambiguous
            else 0.7
            if resolved
            else 0.25
            if ambiguous
            else 0.0,
        }
        return Handoff(facts, self.evidence.refs("get_order", "get_customer_history"))


class OrderAgent:
    actor = "order-agent"

    def __init__(self, evidence: EvidenceBroker) -> None:
        self.evidence = evidence

    async def investigate(self, case: dict[str, Any], entity: dict[str, Any]) -> Handoff:
        order_id = entity["order_id"]
        items_response = await self.evidence.call(self.actor, "get_order_items", order_id=order_id)
        product_response = None
        if case.get("investigation_scope", {}).get("include_product_context"):
            product_response = await self.evidence.call(
                self.actor, "get_product_context", order_id=order_id
            )
        order = entity["order"]
        purchases = purchase_times(order, entity["history"])
        anchor = at(order.get("order_purchase_timestamp"))
        items = analyze_items(
            items_response.get("data") if items_response else None, anchor, purchases
        )
        products = rows(product_response.get("data")) if product_response else []
        items["product_ids"] = sorted(
            {
                str(row["product_id"])
                for row in products
                if row.get("order_item_id") in items["item_ids"] and row.get("product_id")
            }
        )
        items["anchor"] = anchor
        items["purchases"] = purchases
        return Handoff(items, self.evidence.refs("get_order_items", "get_product_context"))


class PaymentAgent:
    actor = "payment-agent"

    def __init__(self, evidence: EvidenceBroker) -> None:
        self.evidence = evidence

    async def investigate(self, order_id: str, order_facts: dict[str, Any]) -> Handoff:
        payments = await self.evidence.call(self.actor, "get_order_payments", order_id=order_id)
        timeline = await self.evidence.call(self.actor, "get_payment_timeline", order_id=order_id)
        refund = await self.evidence.call(self.actor, "get_refund_timeline", order_id=order_id)
        facts = analyze_payment(
            payments.get("data") if payments else None,
            timeline.get("data") if timeline else None,
            refund.get("data") if refund else None,
            order_facts["anchor"],
            order_facts["purchases"],
            order_facts["total"],
        )
        return Handoff(
            facts,
            self.evidence.refs("get_order_payments", "get_payment_timeline", "get_refund_timeline"),
        )


class ShipmentAgent:
    actor = "shipment-agent"

    def __init__(self, evidence: EvidenceBroker) -> None:
        self.evidence = evidence

    async def investigate(self, order_id: str, order_facts: dict[str, Any]) -> Handoff:
        shipment = await self.evidence.call(self.actor, "get_shipment_summary", order_id=order_id)
        sellers = await self.evidence.call(self.actor, "get_sellers", order_id=order_id)
        facts = analyze_shipment(
            shipment.get("data") if shipment else None,
            sellers.get("data") if sellers else None,
            order_facts["anchor"],
            order_facts["purchases"],
        )
        return Handoff(facts, self.evidence.refs("get_shipment_summary", "get_sellers"))


class PolicyAgent:
    actor = "policy-agent"

    def __init__(self, evidence: EvidenceBroker) -> None:
        self.evidence = evidence

    async def investigate(self, version: str) -> Handoff:
        response = await self.evidence.call(self.actor, "get_policy", policy_version=version)
        data = response.get("data") if response else None
        policy = data if isinstance(data, dict) and data.get("policy_version") == version else {}
        return Handoff(policy, self.evidence.refs("get_policy"))
