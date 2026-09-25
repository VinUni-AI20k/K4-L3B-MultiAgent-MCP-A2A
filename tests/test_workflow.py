from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.workflow import (
    InvestigationContext,
    build_case_plan,
    merge_worker_results,
    resolve_entity,
)


class FakeGateway:
    def __init__(self, orders: dict[str, dict[str, Any]]) -> None:
        self.orders = orders
        self.calls: list[tuple[str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        order_id = arguments["order_id"]
        self.calls.append((tool_name, order_id))
        if order_id not in self.orders:
            raise RuntimeError("not found")
        return {"evidence_ref": f"ev_{order_id}{'x' * 24}", "data": self.orders[order_id]}


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def test_build_case_plan_extracts_claims_and_routes_workers() -> None:
    plan = build_case_plan(
        {
            "case_id": "CASE_001",
            "customer_request": {
                "claimed_order_id": "ORDER_2",
                "candidate_order_ids": ["ORDER_1", "ORDER_2", "ORDER_1"],
                "message": "My delivery is late and I need a refund",
                "claims": [
                    {"claim_id": "late", "topic": "shipment"},
                    {"claim_id": "money", "topic": "refund"},
                ],
            },
        }
    )

    assert plan.claimed_order_id == "ORDER_2"
    assert plan.candidate_order_ids == ("ORDER_1", "ORDER_2")
    assert [(claim.claim_id, claim.topic) for claim in plan.claims] == [
        ("late", "shipment"),
        ("money", "refund"),
    ]
    assert plan.workers == (
        "entity-agent",
        "logistics-worker",
        "financial-worker",
        "policy-worker",
        "conflict-resolver",
        "verifier",
    )


def test_entity_resolution_prefers_claimed_order_and_traces_handoff() -> None:
    plan = build_case_plan(
        {
            "case_id": "CASE_002",
            "customer_request": {
                "claimed_order_id": "ORDER_2",
                "candidate_order_ids": ["ORDER_1", "ORDER_2"],
                "customer_unique_id": "CUSTOMER_A",
            },
        }
    )
    gateway = FakeGateway(
        {
            "ORDER_1": {"order_id": "ORDER_1", "customer_unique_id": "CUSTOMER_B"},
            "ORDER_2": {"order_id": "ORDER_2", "customer_unique_id": "CUSTOMER_A"},
        }
    )
    trace = FakeTrace()
    context = InvestigationContext(plan.case_id, gateway, trace)  # type: ignore[arg-type]

    result = asyncio.run(resolve_entity(plan, context))

    assert result.status == "resolved"
    assert result.resolved_order_ids == ("ORDER_2",)
    assert result.rejected_candidates == ("ORDER_1",)
    assert result.confidence == 0.95
    assert [event["event_type"] for event in trace.events] == [
        "task_assigned",
        "tool_result_consumed",
        "tool_result_consumed",
        "handoff",
    ]
    assert trace.events[-1]["target"] == "coordinator"


def test_context_caches_identical_mcp_calls() -> None:
    gateway = FakeGateway({"ORDER_1": {"order_id": "ORDER_1"}})
    trace = FakeTrace()
    context = InvestigationContext("CASE_003", gateway, trace)  # type: ignore[arg-type]

    async def call_twice() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await context.call("entity-agent", "get_order", order_id="ORDER_1")
        second = await context.call("entity-agent", "get_order", order_id="ORDER_1")
        return first, second

    first, second = asyncio.run(call_twice())

    assert first is second
    assert gateway.calls == [("get_order", "ORDER_1")]
    assert len(context.evidence_refs) == 1


def test_merge_worker_results_rejects_overlapping_fields() -> None:
    assert merge_worker_results({"shipment_analysis": {}}, {"payment_analysis": {}}) == {
        "shipment_analysis": {},
        "payment_analysis": {},
    }
    with pytest.raises(ValueError, match="overlapping fields"):
        merge_worker_results({"assessment": {}}, {"assessment": {}})
