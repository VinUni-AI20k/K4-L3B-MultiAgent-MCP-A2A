from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class Gateway:
    async def list_tools(self) -> list[str]:
        return ["get_order", "get_shipment", "get_order_payments", "get_policy"]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        data = {
            "get_order": {"order_id": arguments["order_id"], "customer_unique_id": "customer-1"},
            "get_shipment": {"shipment_id": "shipment-1", "seller_id": "seller-1"},
            "get_order_payments": {"payment_id": "payment-1", "captured_total_brl": 99.5},
            "get_policy": {"policy_id": "standard-1"},
        }[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name:0<20}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order",
            "data": data,
        }


def test_workflow_generates_a_valid_audited_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    result = asyncio.run(
        solve_case({"case_id": "CASE_001", "order_id": "order-1"}, Gateway(), trace)
    )
    contracts.validate_output(result, "test output")
    assert result["entity_resolution"]["status"] == "resolved"
    assert len(result["evidence_refs"]) >= 3
    assert "tool_result_consumed" in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
