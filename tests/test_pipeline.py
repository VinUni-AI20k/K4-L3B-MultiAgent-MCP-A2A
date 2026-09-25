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
