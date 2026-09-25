from __future__ import annotations

from pathlib import Path

from student_agent.contracts import Contracts
from student_agent.pipeline.models import (
    AdjudicationDraft,
    CaseEvidenceContext,
    OrderFindings,
    PaymentFindings,
    ShipmentFindings,
)
from student_agent.pipeline.verifier_agent import VerifierAgent
from student_agent.trace import TraceWriter


def test_verifier_consistency_and_invariants(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    verifier = VerifierAgent(trace, contracts)
    context = CaseEvidenceContext("TEST_CASE_001")
    context.collected_evidence_refs = [f"ev_{'a' * 32}"]

    # Case 1: Late delivery by seller
    order_findings = OrderFindings(
        order_id="order-1",
        seller_ids=["seller-real-1"],
        item_ids=["item-1"],
    )
    shipment_findings = ShipmentFindings(
        order_id="order-1",
        verdict="seller_delay",
        late_seller_ids=["seller-real-1"],
        shipment_ids=["shipment-1"],
    )
    payment_findings = PaymentFindings(
        order_id="order-1",
        verdict="reconciled",
        captured_total_brl=100.0,
    )
    adjudication = AdjudicationDraft(
        primary_issue="late_delivery_seller",
        case_status="action_required",
        confidence=0.92,
        recommended_action="refund_freight",
        recommended_refund_brl=18.0,
        cause_code="LATE_DELIVERY_SELLER",
        responsible_parties=[{"party_type": "seller", "party_id": "seller-real-1"}],
    )

    output = verifier.verify_and_build(
        case={
            "case_id": "TEST_CASE_001",
            "customer_request": {"claims": [{"claim_id": "c1", "topic": "late_delivery_seller"}]},
        },
        resolved_order_id="order-1",
        rejected_candidates=["candidate-1"],
        customer_unique_id="cust-1",
        related_order_ids=["order-1"],
        order_findings=order_findings,
        shipment_findings=shipment_findings,
        payment_findings=payment_findings,
        adjudication=adjudication,
        context=context,
    )

    first_line = output["financial_resolution"]["refund_lines"][0]
    assert first_line["entity_id"] == "seller-real-1"
    assert first_line["reason_code"] == "late_delivery_seller"

    # Case 2: No action status must have 0 refund
    adjudication_no_act = AdjudicationDraft(
        primary_issue="unsupported_claim",
        case_status="no_action",
        confidence=0.90,
        recommended_action="document_no_action",
        recommended_refund_brl=50.0,
        cause_code="UNSUPPORTED_CUSTOMER_CLAIM",
        responsible_parties=[{"party_type": "customer", "party_id": None}],
    )

    output_no_act = verifier.verify_and_build(
        case={
            "case_id": "TEST_CASE_001",
            "customer_request": {"claims": [{"claim_id": "c1", "topic": "unsupported_claim"}]},
        },
        resolved_order_id="order-1",
        rejected_candidates=["candidate-1"],
        customer_unique_id="cust-1",
        related_order_ids=["order-1"],
        order_findings=order_findings,
        shipment_findings=shipment_findings,
        payment_findings=payment_findings,
        adjudication=adjudication_no_act,
        context=context,
    )

    assert output_no_act["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output_no_act["financial_resolution"]["refund_lines"] == []
    assert output_no_act["resolution_actions"] == ["document_no_action"]
