from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from student_agent.a2a import Task, validate_result
from student_agent.agents.shipment import run
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
                ("get_shipment_summary", "order_id"),
                ("get_order_items", "order_id"),
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
            "domain": "shipment",
            "data": {},
        }


async def create_collector(
    gateway: MockGateway | None = None, trace: MockTrace | None = None
) -> EvidenceCollector:
    gw = gateway or MockGateway()
    tr = trace or MockTrace()
    return EvidenceCollector("CASE_001", gw, tr, await gw.discover_tools(), backoff=0)


def test_shipment_on_time() -> None:
    async def scenario() -> None:
        order_id = "ORDER_ON_TIME"
        seller_id = "SELLER_GOOD"

        responses = {
            f"get_shipment_summary:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_ship_12345678901234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "shipment",
                "data": {
                    "order_id": order_id,
                    "shipment_id": "SHIP_001",
                    "status": "delivered",
                    "delivered_carrier_date": "2018-01-02T12:00:00Z",
                    "delivered_customer_date": "2018-01-08T15:00:00Z",
                    "estimated_delivery_date": "2018-01-10T00:00:00Z",
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
                        "seller_id": seller_id,
                        "shipping_limit_date": "2018-01-04T12:00:00Z",
                    }
                ],
            },
        }

        trace = MockTrace()
        c = await create_collector(MockGateway(responses), trace)
        try:
            task = Task("CASE_001", "shipment-agent", "investigate_shipment", {}, (order_id,))
            result = await run(task, c)
            validate_result(task, result, c)

            analysis = result.payload["shipment_analysis"]
            assert analysis["verdict"] == "on_time"
            assert analysis["late_seller_ids"] == []
            assert analysis["timeline_complete"] is True
            assert result.payload["shipment_ids"] == ["SHIP_001"]
            assert seller_id in result.payload["seller_ids"]
        finally:
            await c.close()

    asyncio.run(scenario())


def test_shipment_seller_delay() -> None:
    async def scenario() -> None:
        order_id = "ORDER_SELLER_DELAY"
        seller_id = "SELLER_SLOW"

        responses = {
            f"get_shipment_summary:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_ship_12345678901234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "shipment",
                "data": {
                    "order_id": order_id,
                    "status": "delivered",
                    "delivered_carrier_date": "2018-01-06T12:00:00Z",
                    "delivered_customer_date": "2018-01-12T15:00:00Z",
                    "estimated_delivery_date": "2018-01-10T00:00:00Z",
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
                        "seller_id": seller_id,
                        "shipping_limit_date": "2018-01-04T12:00:00Z",
                    }
                ],
            },
        }

        c = await create_collector(MockGateway(responses))
        try:
            task = Task("CASE_001", "shipment-agent", "investigate_shipment", {}, (order_id,))
            result = await run(task, c)
            validate_result(task, result, c)

            analysis = result.payload["shipment_analysis"]
            assert analysis["verdict"] == "seller_delay"
            assert analysis["late_seller_ids"] == [seller_id]
            assert analysis["timeline_complete"] is True
        finally:
            await c.close()

    asyncio.run(scenario())


def test_shipment_logistics_delay() -> None:
    async def scenario() -> None:
        order_id = "ORDER_LOG_DELAY"
        seller_id = "SELLER_FAST"

        responses = {
            f"get_shipment_summary:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_ship_12345678901234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "shipment",
                "data": {
                    "order_id": order_id,
                    "status": "delivered",
                    "delivered_carrier_date": "2018-01-03T12:00:00Z",
                    "delivered_customer_date": "2018-01-15T15:00:00Z",
                    "estimated_delivery_date": "2018-01-10T00:00:00Z",
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
                        "seller_id": seller_id,
                        "shipping_limit_date": "2018-01-04T12:00:00Z",
                    }
                ],
            },
        }

        c = await create_collector(MockGateway(responses))
        try:
            task = Task("CASE_001", "shipment-agent", "investigate_shipment", {}, (order_id,))
            result = await run(task, c)
            validate_result(task, result, c)

            analysis = result.payload["shipment_analysis"]
            assert analysis["verdict"] == "logistics_delay"
            assert analysis["late_seller_ids"] == []
            assert analysis["timeline_complete"] is True
        finally:
            await c.close()

    asyncio.run(scenario())


def test_shipment_lost_and_returned() -> None:
    async def scenario() -> None:
        order_id = "ORDER_LOST"
        responses = {
            f"get_shipment_summary:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_ship_12345678901234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "shipment",
                "data": {"order_id": order_id, "status": "lost"},
            },
            f"get_order_items:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_items_12345678901234567890",
                "result_hash": "sha256:" + "2" * 64,
                "domain": "item",
                "data": [],
            },
        }

        c = await create_collector(MockGateway(responses))
        try:
            task = Task("CASE_001", "shipment-agent", "investigate_shipment", {}, (order_id,))
            result = await run(task, c)
            validate_result(task, result, c)

            assert result.payload["shipment_analysis"]["verdict"] == "lost"
            assert result.payload["shipment_analysis"]["timeline_complete"] is False
        finally:
            await c.close()

    asyncio.run(scenario())


def test_shipment_conflicting_timeline() -> None:
    async def scenario() -> None:
        order_id = "ORDER_CONFLICT"
        responses = {
            f"get_shipment_summary:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_ship_12345678901234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "shipment",
                "data": {
                    "order_id": order_id,
                    "delivered_carrier_date": "2018-01-10T12:00:00Z",
                    "delivered_customer_date": "2018-01-05T12:00:00Z",  # Delivered before carrier
                    "estimated_delivery_date": "2018-01-15T00:00:00Z",
                },
            },
            f"get_order_items:{order_id}": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_items_12345678901234567890",
                "result_hash": "sha256:" + "2" * 64,
                "domain": "item",
                "data": [],
            },
        }

        c = await create_collector(MockGateway(responses))
        try:
            task = Task("CASE_001", "shipment-agent", "investigate_shipment", {}, (order_id,))
            result = await run(task, c)
            validate_result(task, result, c)

            assert result.payload["shipment_analysis"]["verdict"] == "conflicting"
            assert len(result.payload["data_conflicts"]) > 0
        finally:
            await c.close()

    asyncio.run(scenario())


def test_shipment_tool_permissions() -> None:
    async def scenario() -> None:
        c = await create_collector()
        try:
            with pytest.raises(ValueError, match="permission"):
                await c.call("shipment-agent", "get_order", order_id="ORDER_A")
            with pytest.raises(ValueError, match="permission"):
                await c.call("shipment-agent", "get_policy")
        finally:
            await c.close()

    asyncio.run(scenario())
