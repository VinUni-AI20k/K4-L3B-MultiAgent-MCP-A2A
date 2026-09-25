from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from student_agent.agents import InvestigationContext, OrderProductAgent
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


@pytest.fixture
def mock_context(tmp_path: Path) -> InvestigationContext:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    ctx = InvestigationContext(
        case={
            "case_id": "TEST_CASE_ORDER_001",
            "investigation_scope": {
                "include_product_context": True,
                "include_customer_history": True,
            },
        },
        case_id="TEST_CASE_ORDER_001",
        gateway=MagicMock(),
        trace=trace,
    )
    return ctx


@pytest.mark.anyio
async def test_order_product_agent_financial_aggregation_and_multi_seller(
    mock_context: InvestigationContext,
) -> None:
    # Setup mock cached responses
    order_id = "test_order_123"

    mock_context.cache[f"get_order_items:{sorted([('order_id', order_id)])}"] = {
        "evidence_ref": f"ev_{'a' * 32}",
        "domain": "item",
        "data": [
            {
                "order_item_id": "item-001",
                "product_id": "prod-001",
                "seller_id": "seller-alpha",
                "price": 100.50,
                "freight_value": 15.25,
                "shipping_limit_date": "2026-03-01T12:00:00Z",
            },
            {
                "order_item_id": "item-002",
                "product_id": "prod-002",
                "seller_id": "seller-beta",
                "price": 49.50,
                "freight_value": 10.75,
                "shipping_limit_date": "2026-03-02T12:00:00Z",
            },
        ],
    }

    mock_context.cache[f"get_sellers:{sorted([('order_id', order_id)])}"] = {
        "evidence_ref": f"ev_{'b' * 32}",
        "domain": "seller",
        "data": [
            {
                "seller_id": "seller-alpha",
                "seller_city": "Sao Paulo",
                "seller_state": "SP",
                "seller_zip_code_prefix": "01001",
            },
            {
                "seller_id": "seller-beta",
                "seller_city": "Rio de Janeiro",
                "seller_state": "RJ",
                "seller_zip_code_prefix": "20000",
            },
        ],
    }

    mock_context.cache[f"get_product_context:{sorted([('order_id', order_id)])}"] = {
        "evidence_ref": f"ev_{'c' * 32}",
        "domain": "product",
        "data": [
            {
                "product_id": "prod-001",
                "product_category_name": "eletronicos",
            },
            {
                "product_id": "prod-002",
                "product_category_name": "informatica_acessorios",
            },
        ],
    }

    agent = OrderProductAgent(mock_context)
    result = await agent.run([order_id])

    # Validate IDs
    assert result["item_ids"] == ["item-001", "item-002"]
    assert set(result["seller_ids"]) == {"seller-alpha", "seller-beta"}
    assert result["product_ids"] == ["prod-001", "prod-002"]

    # Validate financial calculations
    assert result["total_items_price"] == 150.00
    assert result["total_freight_value"] == 26.00
    assert result["total_order_value"] == 176.00

    # Validate multi-seller analysis
    analysis = result["order_product_analysis"]
    assert analysis["items_count"] == 2
    assert analysis["distinct_sellers_count"] == 2
    assert analysis["is_multi_seller"] is True
    assert analysis["has_shipping_deadlines"] is True

    # Validate seller financials
    seller_fin = result["seller_financials"]
    assert seller_fin["seller-alpha"]["item_price"] == 100.50
    assert seller_fin["seller-alpha"]["freight"] == 15.25
    assert seller_fin["seller-alpha"]["total"] == 115.75
    assert seller_fin["seller-beta"]["item_price"] == 49.50
    assert seller_fin["seller-beta"]["freight"] == 10.75
    assert seller_fin["seller-beta"]["total"] == 60.25

    # Validate seller locations and product categories
    assert result["seller_locations"]["seller-alpha"]["city"] == "Sao Paulo"
    assert result["seller_locations"]["seller-beta"]["state"] == "RJ"
    assert "eletronicos" in result["product_categories"]
    assert "informatica_acessorios" in result["product_categories"]


@pytest.mark.anyio
async def test_order_product_agent_empty_order_resilience(
    mock_context: InvestigationContext,
) -> None:
    agent = OrderProductAgent(mock_context)
    result = await agent.run([])

    assert result["item_ids"] == []
    assert result["seller_ids"] == []
    assert result["total_items_price"] == 0.0
    assert result["total_freight_value"] == 0.0
    assert result["total_order_value"] == 0.0
    assert result["order_product_analysis"]["is_multi_seller"] is False
