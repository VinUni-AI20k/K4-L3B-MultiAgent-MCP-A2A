"""Conflict -> policy -> verifier chain on synthetic cases (no competition data, no network)."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from student_agent.a2a import CaseContext
from student_agent.agents.verifier import verify
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import Coordinator
from test_specialists import ORDER_ID, SELLER, CannedGateway, responses

ROOT = Path(__file__).resolve().parents[1]
POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V2",
    "rules": {
        "late_delivery_logistics": {
            "case_status": "action_required", "recommended_action": "refund_freight",
            "refund_brl": 16.0,
            "responsible_parties": [{"party_id": None, "party_type": "logistics_provider"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required", "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-from-other-case", "party_type": "seller"}],
        },
        "unsupported_claim": {
            "case_status": "no_action", "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    },
}  # fmt: skip


def solve(tmp_path: Path, canned: dict[str, Any], **case_overrides: Any):
    case = {
        "case_id": "L3B_CASE_TEST",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": [ORDER_ID, "candidate-test"],
        "investigation_scope": {"include_product_context": True},
        "customer_unique_id_hint": "customer-hint",
        **case_overrides,
    }
    canned = {**canned, "get_policy": POLICY}
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    ctx = CaseContext(case, CannedGateway(canned), TraceWriter(trace_path, contracts))  # type: ignore[arg-type]
    output = asyncio.run(Coordinator().run(ctx))
    contracts.validate_output(output, "output")
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    return output, events, ctx


def test_end_to_end_output_is_scoped_consistent_and_traced(tmp_path: Path) -> None:
    output, events, ctx = solve(tmp_path, responses())
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"
    # min(freight 18, refundable 16) and matches the policy amount
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "logistics_provider", "party_id": None}
    ]
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-test"]
    # a freight refund never settles a full-refund request
    assert [claim["verdict"] for claim in output["claim_assessments"]] == [
        "supported",
        "partially_supported",
    ]

    consumed = {ref for e in events if e["event_type"] == "tool_result_consumed"
                for ref in e["evidence_refs"]}  # fmt: skip
    assert set(output["evidence_refs"]) <= consumed
    # a late delivery is proven by shipment and item evidence; payment evidence is not cited.
    # get_order stays cited even though it shows another episode: order evidence is required.
    cited = {ctx.evidence[ref].tool_name for ref in output["evidence_refs"]}
    assert cited == {
        "get_order", "get_customer_history", "get_policy", "get_shipment_summary",
        "get_order_items",
    }  # fmt: skip
    assert output["assessment"]["confidence"] == 0.99
    kinds = [event["event_type"] for event in events]
    assert kinds.index("policy_decided") < kinds.index("verification_completed")
    assert kinds[-1] == "handoff" and events[-1]["target"] == "coordinator"
    assert {event["actor"] for event in events} >= {
        "coordinator", "entity-agent", "order-agent", "shipment-agent", "payment-agent",
        "conflict-agent", "policy-agent", "verifier-agent",
    }  # fmt: skip
    # order, customer history, items, shipment, payment timeline, policy; no product/refund
    assert ctx.calls_made == 6


def test_evidence_overrides_contradicting_claim(tmp_path: Path) -> None:
    request = {
        "claimed_order_id": ORDER_ID,
        "claims": [{"claim_id": "claim-a", "topic": "late_delivery_seller"}],
    }
    output, _, _ = solve(tmp_path, responses(), customer_request=request)
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] < 0.9


def test_seller_party_comes_from_case_not_policy(tmp_path: Path) -> None:
    canned = responses()
    late = copy.deepcopy(canned["get_customer_history"]["orders"][1])
    late["order_delivered_carrier_date"] = "2017-12-26T09:00:00-03:00"
    canned["get_customer_history"]["orders"][1] = late
    canned["get_shipment_summary"]["events"][1]["actor"] = "seller"
    output, _, _ = solve(tmp_path, canned)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER}
    ]


def test_unresolved_entity_yields_needs_investigation(tmp_path: Path) -> None:
    request = {"claimed_order_id": "candidate-x", "claims": []}
    output, _, ctx = solve(
        tmp_path, responses(), customer_request=request, candidate_order_ids=["candidate-x"]
    )
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["assessment"]["confidence"] <= 0.4
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert ctx.calls_made == 2  # customer history + policy; no order lookups for placeholders


def test_verifier_repairs_inconsistent_draft(tmp_path: Path) -> None:
    output, _, ctx = solve(tmp_path, responses())
    broken = copy.deepcopy(output)
    broken["assessment"]["case_status"] = "no_action"
    broken["resolution_actions"] = ["refund_freight", "refund_freight"]
    broken["evidence_refs"].append("ev_never_returned_by_mcp_000")
    broken["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "seller", "party_id": "seller-from-other-case"}
    ]
    corrections = verify(ctx, broken)
    assert {"UNTRACED_EVIDENCE", "DUPLICATE_ACTIONS", "NO_ACTION_WITH_REFUND",
            "SELLER_PARTY_SCOPE"} <= set(corrections)  # fmt: skip
    assert broken["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 0.0,
        "refund_lines": [],
    }
    assert broken["root_cause_analysis"]["responsible_parties"][0]["party_id"] == SELLER
    assert "ev_never_returned_by_mcp_000" not in broken["evidence_refs"]
    assert broken["assessment"]["confidence"] < output["assessment"]["confidence"]
