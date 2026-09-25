from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from student_agent.contracts import Contracts
from student_agent.pipeline.models import CaseEvidenceContext
from student_agent.pipeline.order_agent import OrderAgent
from student_agent.trace import TraceWriter


@pytest.fixture
def mock_setup(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = MagicMock()
    context = CaseEvidenceContext("TEST_CASE_ORDER_001")
    agent = OrderAgent(gateway, trace)
    return agent, context


@pytest.mark.anyio
async def test_order_agent_financial_aggregation_and_multi_seller(mock_setup) -> None:
    agent, context = mock_setup
    order_id = "test_order_123"

    context.set_cached(
        "get_order_items",
        {"order_id": order_id},
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{'a' * 32}",
            "result_hash": f"sha256:{'0' * 64}",
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
        },
    )

    context.set_cached(
        "get_sellers",
        {"order_id": order_id},
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{'b' * 32}",
            "result_hash": f"sha256:{'0' * 64}",
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
        },
    )

    context.set_cached(
        "get_product_context",
        {"order_id": order_id},
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{'c' * 32}",
            "result_hash": f"sha256:{'0' * 64}",
            "domain": "product",
            "data": [
                {"product_id": "prod-001", "category_name_english": "eletronicos"},
                {"product_id": "prod-002", "category_name_english": "informatica_acessorios"},
            ],
        },
    )

    findings = await agent.investigate(
        case_id="TEST_CASE_ORDER_001",
        order_id=order_id,
        scope={"include_product_context": True},
        context=context,
    )

    # Validate findings
    assert findings.item_ids == ["item-001", "item-002"]
    assert set(findings.seller_ids) == {"seller-alpha", "seller-beta"}
    assert findings.total_items_price_brl == 150.00
    assert findings.total_freight_brl == 26.00
    assert findings.total_order_value_brl == 176.00
    assert findings.seller_financials["seller-alpha"]["item_price"] == 100.50
    assert findings.seller_financials["seller-alpha"]["freight"] == 15.25
    assert findings.seller_financials["seller-beta"]["item_price"] == 49.50
    assert findings.seller_financials["seller-beta"]["freight"] == 10.75
    assert findings.seller_locations["seller-alpha"]["city"] == "Sao Paulo"
    assert findings.seller_locations["seller-beta"]["state"] == "RJ"
    assert "eletronicos" in findings.product_categories
    assert "informatica_acessorios" in findings.product_categories
    assert len(findings.shipping_deadlines) == 2


@pytest.mark.anyio
async def test_order_agent_resilience(mock_setup) -> None:
    agent, context = mock_setup
    findings = await agent.investigate(
        case_id="TEST_CASE_EMPTY",
        order_id="unknown_order",
        scope={},
        context=context,
    )
    assert findings.item_ids == []
    assert findings.seller_ids == []
    assert findings.total_items_price_brl == 0.0
    assert findings.total_freight_brl == 0.0
    assert findings.total_order_value_brl == 0.0
