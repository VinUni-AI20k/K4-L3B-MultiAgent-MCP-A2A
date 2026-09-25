from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


class FakeSession:
    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        assert name == "get_order"
        assert arguments == {"case_id": "L3B_CASE_001", "order_id": "order-1"}
        return CallToolResult(
            content=[],
            structuredContent={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_" + "a" * 22,
                "result_hash": "sha256:" + "a" * 64,
                "domain": "order",
                "data": {"order_id": "order-1"},
            },
        )


def test_call_reads_mcp_sdk_response_fields() -> None:
    root = Path(__file__).resolve().parents[1]
    gateway = EvidenceGateway(FakeSession(), Contracts(root / "contracts" / "schemas"))
    result = asyncio.run(gateway.call("get_order", case_id="L3B_CASE_001", order_id="order-1"))
    assert result["data"]["order_id"] == "order-1"


def test_tool_discovery_is_cached_for_the_session() -> None:
    class ListingSession:
        def __init__(self) -> None:
            self.calls = 0

        async def list_tools(self):
            self.calls += 1
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="get_order",
                        input_schema={"type": "object"},
                        output_schema={"type": "object"},
                    )
                ]
            )

    root = Path(__file__).resolve().parents[1]
    session = ListingSession()
    gateway = EvidenceGateway(session, Contracts(root / "contracts" / "schemas"))
    assert asyncio.run(gateway.list_tools()) == ["get_order"]
    assert asyncio.run(gateway.describe_tools())[0]["name"] == "get_order"
    assert asyncio.run(gateway.describe_tools())[0]["name"] == "get_order"
    assert session.calls == 1
