from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from student_agent.agents import InvestigationContext, VerifierAgent
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


def test_cross_field_consistency_and_confidence_calibration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    ctx = InvestigationContext(
        case={"case_id": "TEST_CASE_001"},
        case_id="TEST_CASE_001",
        gateway=MagicMock(),
        trace=trace,
    )
    ctx.all_evidence_refs = [f"ev_{'a' * 32}"] * 5

    verifier = VerifierAgent(ctx)

    # Test Case 1: Late delivery by logistics provider
    # Seller should NOT be the entity charged in refund_lines!
    policy_output_logistics = {
        "assessment": {
            "primary_issue": "late_delivery_logistics",
            "secondary_issues": ["requested_full_refund"],
            "case_status": "action_required",
            "confidence": 0.90,
        },
        "claim_assessments": [
            {"claim_id": "c1", "verdict": "supported", "confidence": 0.9, "evidence_refs": []}
        ],
        "data_conflicts": [
            {"field": "refund_amount", "sources": ["a", "b"], "selected_source": "b", "resolution_code": "code"}
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "LATE_DELIVERY_LOGISTICS", "rank": 1}],
            "responsible_parties": [{"party_type": "logistics_provider", "party_id": "shipment-test"}],
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 16.0,
            "refund_lines": [{"reason_code": "late_delivery_logistics", "amount_brl": 16.0, "entity_id": "seller-wrong"}],
        },
        "resolution_actions": ["refund_freight", "notify_customer"],
    }

    verified = verifier.verify_and_finalize(
        entity_output={"entity_resolution": {"status": "resolved", "resolved_order_ids": ["order-1"]}},
        order_output={"seller_ids": ["seller-real-1"], "item_ids": ["item-1"]},
        shipment_output={
            "shipment_analysis": {"verdict": "logistics_delay", "late_seller_ids": [], "timeline_complete": True},
            "shipment_ids": ["shipment-test"],
        },
        payment_output={
            "payment_analysis": {"verdict": "reconciled", "refundable_total_brl": 100.0},
            "payment_references": ["pay-1"],
        },
        policy_output=policy_output_logistics,
    )

    # Check that seller is NOT responsible for logistics delay refund
    assert verified["financial_resolution"]["refund_lines"][0]["entity_id"] == "shipment-test"
    assert verified["financial_resolution"]["refund_lines"][0]["entity_id"] != "seller-real-1"
    # Check that confidence was calibrated down due to data conflict (should not be 1.0 or overconfident)
    assert verified["assessment"]["confidence"] < 0.92
    assert 0.35 <= verified["assessment"]["confidence"] <= 0.95

    # Test Case 2: No action status must have 0 refund
    policy_output_no_action = {
        "assessment": {
            "primary_issue": "unsupported_claim",
            "secondary_issues": [],
            "case_status": "no_action",
            "confidence": 0.90,
        },
        "claim_assessments": [],
        "data_conflicts": [],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "UNSUPPORTED_CLAIM", "rank": 1}],
            "responsible_parties": [{"party_type": "customer", "party_id": None}],
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 50.0,
            "refund_lines": [{"reason_code": "unsupported_claim", "amount_brl": 50.0, "entity_id": "seller-1"}],
        },
        "resolution_actions": ["issue_refund"],
    }

    verified_no_act = verifier.verify_and_finalize(
        entity_output={"entity_resolution": {"status": "resolved", "resolved_order_ids": ["order-1"]}},
        order_output={"seller_ids": ["seller-real-1"], "item_ids": ["item-1"]},
        shipment_output={
            "shipment_analysis": {"verdict": "on_time", "late_seller_ids": [], "timeline_complete": True},
            "shipment_ids": ["shipment-test"],
        },
        payment_output={
            "payment_analysis": {"verdict": "reconciled", "refundable_total_brl": 100.0},
            "payment_references": ["pay-1"],
        },
        policy_output=policy_output_no_action,
    )

    assert verified_no_act["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert verified_no_act["financial_resolution"]["refund_lines"] == []
    assert verified_no_act["resolution_actions"] == ["document_no_action"]


def test_verifier_self_healing_and_telemetry(tmp_path: Path) -> None:
    import json
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    ctx = InvestigationContext(
        case={"case_id": "TEST_CASE_002", "investigation_scope": {"require_independent_verification": True}},
        case_id="TEST_CASE_002",
        gateway=MagicMock(),
        trace=trace,
    )
    ctx.all_evidence_refs = [f"ev_{'b' * 32}"] * 8

    verifier = VerifierAgent(ctx)

    # Input has:
    # 1. Overlap between resolved_order_ids and rejected_candidates
    # 2. Refund exceeds refundable total
    # 3. Line item amount does not match recommended refund
    # 4. Ranked causes non-contiguous ranks
    entity_output = {
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["order-1"],
            "rejected_candidates": ["order-1", "order-candidate-bad"],
        }
    }
    policy_output = {
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "secondary_issues": [],
            "case_status": "action_required",
            "confidence": 0.88,
        },
        "claim_assessments": [],
        "data_conflicts": [],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "LATE_DELIVERY_SELLER", "rank": 5}],
            "responsible_parties": [{"party_type": "seller", "party_id": "seller-1"}],
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 150.0,  # Exceeds refundable 80.0
            "refund_lines": [{"reason_code": "late_delivery_seller", "amount_brl": 150.0, "entity_id": "seller-1"}],
        },
        "resolution_actions": ["refund_freight", "refund_freight", "notify_customer"],
    }

    verified = verifier.verify_and_finalize(
        entity_output=entity_output,
        order_output={"seller_ids": ["seller-1"], "item_ids": ["item-1"]},
        shipment_output={
            "shipment_analysis": {"verdict": "seller_delay", "late_seller_ids": ["seller-1"], "timeline_complete": True},
            "shipment_ids": ["shipment-1"],
        },
        payment_output={
            "payment_analysis": {"verdict": "reconciled", "refundable_total_brl": 80.0},
            "payment_references": ["pay-1"],
        },
        policy_output=policy_output,
    )

    # 1. Candidate disjointness enforced
    assert "order-1" not in entity_output["entity_resolution"]["rejected_candidates"]
    assert entity_output["entity_resolution"]["rejected_candidates"] == ["order-candidate-bad"]

    # 2. Refund capped to refundable_total_brl
    assert verified["financial_resolution"]["recommended_refund_brl"] == 80.0
    assert verified["financial_resolution"]["refund_lines"][0]["amount_brl"] == 80.0

    # 3. Actions deduplicated
    assert verified["resolution_actions"] == ["refund_freight", "notify_customer"]

    # 4. Contiguous ranking fixed
    assert verified["root_cause_analysis"]["ranked_causes"][0]["rank"] == 1

    # 5. Check trace telemetry event
    events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    verify_events = [e for e in events if e["event_type"] == "verification_completed"]
    assert len(verify_events) == 1
    attrs = verify_events[0]["attributes"]
    assert attrs["status"] == "passed"
    assert attrs["invariants_checked"] == 10
    assert attrs["anomalies_repaired"] >= 2

