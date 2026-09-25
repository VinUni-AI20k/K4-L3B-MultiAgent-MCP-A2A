from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from student_agent.a2a import Task, validate_result
from student_agent.agents.order import run
from student_agent.contracts import Contracts
from student_agent.evidence_collector import EvidenceCollector


class MockTrace:
    def __init__(self) -> None:
        self.contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


class MockGateway:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self.responses = responses or {}

    async def discover_tools(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "type": "object",
                "properties": {"case_id": {"type": "string"}, arg: {"type": "string"}},
                "required": ["case_id", arg],
                "additionalProperties": False,
            }
            for name, arg in [
                ("get_order", "order_id"),
                ("get_order_items", "order_id"),
                ("get_product_context", "product_id"),
                ("get_sellers", "seller_id"),
            ]
        }

    async def call(self, tool: str, *, case_id: str, **args: Any) -> dict[str, Any]:
        self.calls += 1
        key = f"{tool}:{list(args.values())[0] if args else ''}"
        if key in self.responses:
            return self.responses[key]
        if tool in self.responses:
            return self.responses[tool]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{case_id}_{tool}_{self.calls}".ljust(24, "_"),
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {},
        }


async def create_collector(
    gateway: MockGateway | None = None, trace: MockTrace | None = None
) -> EvidenceCollector:
    gw = gateway or MockGateway()
    tr = trace or MockTrace()
    return EvidenceCollector("CASE_001", gw, tr, await gw.discover_tools(), backoff=0)


def test_order_agent_success() -> None:
    async def scenario() -> None:
        order_id = "ORDER_123"
        seller_id = "SELLER_456"
        product_id = "PROD_789"

        responses = {
            f"get_order:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_order_12345678901234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "order",
                "data": {
                    "order_id": order_id,
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-01-01 10:00:00",
                },
            },
            f"get_order_items:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_items_12345678901234567890",
                "result_hash": "sha256:" + "2" * 64,
                "domain": "item",
                "data": [
                    {
                        "order_id": order_id,
                        "order_item_id": 1,
                        "product_id": product_id,
                        "seller_id": seller_id,
                        "shipping_limit_date": "2018-01-04 12:00:00",
                    }
                ],
            },
            f"get_product_context:{product_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_prod_123456789012345678900",
                "result_hash": "sha256:" + "3" * 64,
                "domain": "product",
                "data": {"product_id": product_id, "category": "electronics"},
            },
            f"get_sellers:{seller_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_sell_123456789012345678900",
                "result_hash": "sha256:" + "4" * 64,
                "domain": "seller",
                "data": {"seller_id": seller_id, "city": "sao paulo"},
            },
        }

        trace = MockTrace()
        c = await create_collector(MockGateway(responses), trace)
        try:
            task = Task(
                case_id="CASE_001",
                recipient="order-agent",
                task_type="investigate_order",
                payload={"case": {}, "entity": {}},
                entity_scope=(order_id,),
            )
            result = await run(task, c)
            validate_result(task, result, c)

            assert result.status == "completed"
            assert result.payload["order_ids"] == [order_id]
            assert result.payload["seller_ids"] == [seller_id]
            assert len(result.payload["orders"]) == 1
            assert len(result.payload["items"]) == 1
            assert len(result.evidence_refs) >= 2

            consumed = [e for e in trace.events if e.get("event_type") == "tool_result_consumed"]
            assert len(consumed) >= 2
            assert all(e["actor"] == "order-agent" for e in consumed)
        finally:
            await c.close()

    asyncio.run(scenario())


def test_order_agent_empty_scope() -> None:
    async def scenario() -> None:
        c = await create_collector()
        try:
            task = Task(
                case_id="CASE_001",
                recipient="order-agent",
                task_type="investigate_order",
                payload={"case": {}, "entity": {}},
                entity_scope=(),
            )
            result = await run(task, c)
            validate_result(task, result, c)

            assert result.status == "completed"
            assert result.payload["order_ids"] == []
            assert result.evidence_refs == ()
        finally:
            await c.close()

    asyncio.run(scenario())


def test_order_agent_permission_rejection() -> None:
    async def scenario() -> None:
        c = await create_collector()
        try:
            with pytest.raises(ValueError, match="permission"):
                await c.call("order-agent", "get_policy")
            with pytest.raises(ValueError, match="permission"):
                await c.call("order-agent", "get_order_payments", order_id="ORDER_A")
        finally:
            await c.close()

    asyncio.run(scenario())
