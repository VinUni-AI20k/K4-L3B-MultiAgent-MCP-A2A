"""Unit tests for the Google ADK Multi-Agent pipeline."""

import pytest

from student_agent.pipeline.models import (
    A2AMessage,
    CaseEvidenceContext,
    OrderFindings,
    PaymentFindings,
    ShipmentFindings,
)


def test_a2a_message_creation_and_hop():
    msg = A2AMessage(
        case_id="L3B_CASE_001",
        sender="coordinator",
        recipient="order_agent",
        intent="task_assign",
        payload={"task": "inspect_order"},
    )
    assert msg.hop_count == 0
    assert msg.sender == "coordinator"
    assert msg.recipient == "order_agent"

    next_msg = msg.next_hop(
        recipient="policy_agent",
        intent="handoff",
        payload={"order_findings": "ok"},
    )
    assert next_msg.hop_count == 1
    assert next_msg.sender == "order_agent"
    assert next_msg.recipient == "policy_agent"


def test_a2a_cycle_prevention():
    msg = A2AMessage(
        case_id="L3B_CASE_001",
        sender="agent_a",
        recipient="agent_b",
        intent="task_assign",
        payload={},
        hop_count=9,
    )
    next_msg = msg.next_hop("agent_c", "handoff", {})
    assert next_msg.hop_count == 10

    with pytest.raises(RuntimeError, match="A2A cycle detected"):
        next_msg.next_hop("agent_a", "handoff", {})


def test_case_evidence_context_caching():
    ctx = CaseEvidenceContext("L3B_CASE_001")
    assert ctx.get_cached("get_order", {"order_id": "123"}) is None

    fake_ev = {"evidence_ref": "ev_1234567890123456789012", "data": {"status": "ok"}}
    ctx.set_cached("get_order", {"order_id": "123"}, fake_ev)

    cached = ctx.get_cached("get_order", {"order_id": "123"})
    assert cached == fake_ev
    assert "ev_1234567890123456789012" in ctx.collected_evidence_refs


def test_order_findings_defaults():
    of = OrderFindings(order_id="test_order")
    assert of.order_id == "test_order"
    assert of.total_items_price_brl == 0.0
    assert of.item_ids == []


def test_shipment_findings_verdict():
    sf = ShipmentFindings(order_id="test_order", verdict="on_time")
    assert sf.verdict == "on_time"
    assert sf.is_seller_delay is False


def test_payment_findings_reconciliation():
    pf = PaymentFindings(order_id="test_order", verdict="reconciled")
    assert pf.captured_total_brl == 0.0
    assert pf.refundable_total_brl == 0.0


@pytest.mark.anyio
async def test_shipment_agent_milestone_evaluation(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock

    from student_agent.contracts import Contracts
    from student_agent.pipeline.shipment_agent import ShipmentAgent
    from student_agent.trace import TraceWriter

    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "traces" / "trace.jsonl", contracts)
    agent = ShipmentAgent(MagicMock(), trace)
    ctx = CaseEvidenceContext("TEST_CASE_SHIP_001")

    # Mock get_shipment_summary with seller delivered_late event
    ctx.set_cached(
        "get_shipment_summary",
        {"order_id": "ord_1"},
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{'s' * 32}",
            "result_hash": f"sha256:{'1' * 64}",
            "domain": "shipment",
            "data": {
                "order_id": "ord_1",
                "order_status": "delivered",
                "delivered_carrier_at": "2018-05-13T09:00:00-03:00",
                "delivered_customer_at": "2018-05-20T09:00:00-03:00",
                "estimated_delivery_at": "2018-05-21T09:00:00-03:00",
                "shipping_limits": [
                    {
                        "order_item_id": "item-1",
                        "seller_id": "seller-real-1",
                        "shipping_limit_at": "2018-05-14T09:00:00-03:00",
                    }
                ],
                "events": [
                    {
                        "order_id": "ord_1",
                        "event_at": "2018-05-14T09:00:00-03:00",
                        "event_type": "delivered_late",
                        "actor": "seller",
                        "status": "confirmed",
                    }
                ],
            },
        },
    )

    findings = await agent.investigate("TEST_CASE_SHIP_001", "ord_1", ctx)
    assert findings.verdict == "seller_delay"
    assert findings.is_seller_delay is True
    assert findings.late_seller_ids == ["seller-real-1"]


@pytest.mark.anyio
async def test_payment_agent_duplicate_and_selective_refund(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock

    from student_agent.contracts import Contracts
    from student_agent.pipeline.payment_agent import PaymentAgent
    from student_agent.trace import TraceWriter

    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "traces" / "trace.jsonl", contracts)
    agent = PaymentAgent(MagicMock(), trace)
    ctx = CaseEvidenceContext("TEST_CASE_PAY_001")

    # Mock get_order_payments with duplicate payment sequentials
    ctx.set_cached(
        "get_order_payments",
        {"order_id": "ord_pay_1"},
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{'p' * 32}",
            "result_hash": f"sha256:{'2' * 64}",
            "domain": "payment",
            "data": [
                {
                    "payment_sequential": "1",
                    "payment_type": "credit_card",
                    "payment_value": "64.00",
                },
                {
                    "payment_sequential": "2",
                    "payment_type": "voucher",
                    "payment_value": "64.00",
                },
                {
                    "payment_sequential": "1",
                    "payment_type": "credit_card",
                    "payment_value": "64.00",
                },
                {
                    "payment_sequential": "2",
                    "payment_type": "voucher",
                    "payment_value": "64.00",
                },
            ],
        },
    )
    ctx.set_cached(
        "get_payment_timeline",
        {"order_id": "ord_pay_1"},
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{'t' * 32}",
            "result_hash": f"sha256:{'3' * 64}",
            "domain": "payment",
            "data": {"events": []},
        },
    )

    findings = await agent.investigate(
        "TEST_CASE_PAY_001",
        "ord_pay_1",
        ctx,
        claimed_topics=["duplicate_charge"],
    )
    assert findings.verdict == "duplicate_capture"
    assert findings.has_duplicate_capture is True
    assert findings.captured_total_brl == 256.0
