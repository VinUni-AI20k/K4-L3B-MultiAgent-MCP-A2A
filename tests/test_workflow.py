from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


@pytest.mark.asyncio
async def test_solve_case_offline_mock(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    sample_case = {
        "case_id": "L3B_CASE_001",
        "claims": [
            {
                "claim_id": "claim_01",
                "text": "My package was delayed by the seller and never delivered on time.",
            }
        ],
        "candidate_order_ids": [
            "e481f51cbd15022285c3c0534f1f14dd",
            "candidate-invalid-999",
        ],
        "hints": {"customer_unique_id": "cust_871766c2855e8b924ed7486e90dd38db"},
    }

    # Mock gateway
    gateway = MagicMock()
    gateway.list_tools = AsyncMock(
        return_value=[
            "get_customer_history",
            "get_order_details",
            "get_shipment_status",
            "get_payment_details",
            "get_order_items",
            "get_policy",
        ]
    )
    gateway.call = AsyncMock(
        side_effect=lambda tool_name, case_id, **kwargs: {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_0123456789abcdef0123456789abcdef",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "shipment" if "shipment" in tool_name else "order",
            "data": {
                "order_purchase_timestamp": "2023-01-01T10:00:00Z",
                "order_delivered_carrier_date": "2023-01-10T10:00:00Z",
                "shipping_limit_date": "2023-01-05T10:00:00Z",
                "order_delivered_customer_date": "2023-01-15T10:00:00Z",
                "order_estimated_delivery_date": "2023-01-12T10:00:00Z",
                "seller_id": "seller_abc123",
                "payment_value": 120.50,
                "price": 100.00,
                "freight_value": 20.50,
            },
        }
    )

    output = await solve_case(sample_case, gateway, trace)

    # Validate output contract
    contracts.validate_output(output, "test_output")

    assert output["case_id"] == "L3B_CASE_001"
    assert "candidate-invalid-999" in output["entity_resolution"]["rejected_candidates"]
    assert "e481f51cbd15022285c3c0534f1f14dd" in output["entity_resolution"]["resolved_order_ids"]
    assert output["assessment"]["case_status"] in ["action_required", "no_action", "needs_investigation"]
