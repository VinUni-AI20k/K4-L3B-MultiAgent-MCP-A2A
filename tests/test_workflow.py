from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.workflow import (
    ConflictResolution,
    EntityResolution,
    InvestigationContext,
    PolicyAnalysis,
    build_case_plan,
    build_final_output,
    merge_worker_results,
    resolve_entity,
)


class FakeGateway:
    def __init__(self, orders: dict[str, dict[str, Any]]) -> None:
        self.orders = orders
        self.calls: list[tuple[str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if tool_name == "get_customer_history":
            return {
                "evidence_ref": "ev_" + "h" * 24,
                "data": {
                    "orders": [
                        row
                        for row in self.orders.values()
                        if row.get("customer_unique_id") == arguments["customer_unique_id"]
                    ]
                },
            }
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


def test_gateway_failure_does_not_become_order_not_found() -> None:
    plan = build_case_plan(
        {"case_id": "CASE_ERROR", "customer_request": {"claimed_order_id": "ORDER_1"}}
    )
    trace = FakeTrace()
    context = InvestigationContext(plan.case_id, FakeGateway({}), trace)
    with pytest.raises(RuntimeError):
        asyncio.run(resolve_entity(plan, context))
    assert len(context.failures) == 1
    assert all(event["event_type"] != "handoff" for event in trace.events)


def test_failed_run_preserves_existing_outputs(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from student_agent import cli

    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    output = tmp_path / "outputs" / "CASE_001.json"
    trace = tmp_path / "traces" / "trace.jsonl"
    output.write_text("previous output")
    trace.write_text("previous trace")
    monkeypatch.setattr(cli.Settings, "load", lambda root: None)
    monkeypatch.setattr(
        cli,
        "load_case_set",
        lambda root: SimpleNamespace(case_ids=("CASE_001",), version="test-v1"),
    )
    monkeypatch.setattr(cli, "Contracts", lambda path: None)

    async def fail(*args):
        raise RuntimeError("Gateway unavailable")

    async def ensure(*args):
        return None

    monkeypatch.setattr(cli, "_ensure_run", ensure)

    monkeypatch.setattr(cli, "_generate_run", fail)
    with pytest.raises(RuntimeError, match="Gateway unavailable"):
        asyncio.run(cli._run(tmp_path))
    assert output.read_text() == "previous output"
    assert trace.read_text() == "previous trace"


def test_nested_mcp_error_is_readable() -> None:
    from student_agent.cli import _error_message

    error = ExceptionGroup(
        "transport",
        [
            ExceptionGroup(
                "session",
                [RuntimeError("MCP tool get_order failed: Error executing tool get_order")],
            )
        ],
    )
    assert _error_message(error) == ("MCP tool get_order failed: Error executing tool get_order")


def test_policy_uses_version_and_customer_hint() -> None:
    from student_agent.workflow import run_policy_worker

    plan = build_case_plan(
        {
            "case_id": "CASE_POLICY",
            "customer_unique_id_hint": "CUSTOMER_A",
            "policy_version": "EC_POLICY_V2",
        }
    )
    assert plan.customer_unique_id == "CUSTOMER_A"

    class PolicyGateway:
        async def call(self, tool_name: str, *, case_id: str, **arguments: str):
            assert tool_name == "get_policy"
            assert case_id == "CASE_POLICY"
            assert arguments == {"policy_version": "EC_POLICY_V2"}
            return {"evidence_ref": "ev_" + "p" * 24, "data": {}}

    context = InvestigationContext(plan.case_id, PolicyGateway(), FakeTrace())
    entity = EntityResolution("resolved", ("ORDER_1",), (), 0.8, ())
    result = asyncio.run(run_policy_worker(plan, context, entity))
    assert result.evidence_refs == ("ev_" + "p" * 24,)


def test_output_uses_primary_claim_and_policy_rule() -> None:
    plan = build_case_plan(
        {
            "case_id": "CASE_POLICY",
            "customer_unique_id_hint": "CUSTOMER_A",
            "policy_version": "EC_POLICY_V2",
            "customer_request": {
                "claims": [
                    {"claim_id": "a", "topic": "late_delivery_logistics"},
                    {"claim_id": "b", "topic": "requested_full_refund"},
                ]
            },
        }
    )
    context = InvestigationContext(plan.case_id, FakeGateway({}), FakeTrace())
    context.evidence_refs.append("ev_" + "e" * 24)
    policy = PolicyAnalysis(
        "partially_supported",
        None,
        (
            {
                "claim_id": "a",
                "verdict": "supported",
                "confidence": 0.85,
                "evidence_refs": ["ev_" + "e" * 24],
            },
            {
                "claim_id": "b",
                "verdict": "insufficient_evidence",
                "confidence": 0.3,
                "evidence_refs": ["ev_" + "e" * 24],
            },
        ),
        ("ev_" + "e" * 24,),
        (),
        {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 16.0,
            "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
        },
    )
    output = build_final_output(
        plan,
        context,
        EntityResolution("resolved", ("ORDER_1",), (), 0.95, ()),
        {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False},
        {
            "payment_analysis": {
                "verdict": "reconciled",
                "captured_total_brl": 105.0,
                "refunded_total_brl": 0.0,
                "refundable_total_brl": 105.0,
            },
            "financial_resolution": {},
        },
        policy,
        ConflictResolution([], [], []),
    )
    assert output["assessment"] == {
        "primary_issue": "late_delivery_logistics",
        "secondary_issues": ["requested_full_refund"],
        "case_status": "action_required",
        "confidence": 0.95,
    }
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"
