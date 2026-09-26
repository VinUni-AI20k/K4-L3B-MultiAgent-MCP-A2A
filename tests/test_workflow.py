from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from student_agent.cases import CaseSet, load_case_set, prepare_inputs
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.workflow import Investigation, solve_case, verify_output

ROOT = Path(__file__).resolve().parents[1]
REF = "ev_" + "a" * 24
CASE = {
    "case_id": "CASE_001",
    "candidate_order_ids": ["order-a", "order-b"],
    "customer_request": {"claims": [{"claim_id": "claim-a", "topic": "refund"}]},
}
EVIDENCE = {
    "schema_version": "day09-mcp-evidence-v1",
    "evidence_ref": REF,
    "result_hash": "sha256:" + "0" * 64,
    "domain": "order",
    "data": {"order_id": "order-a", "customer_unique_id": "customer-a"},
}
TOOL = {
    "name": "get_order",
    "description": "Read an order",
    "inputSchema": {
        "type": "object",
        "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
        "required": ["case_id", "order_id"],
        "additionalProperties": False,
    },
}


def output():
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "CASE_001",
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.3,
        },
        "affected_entities": {
            "order_ids": ["order-a"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["order-a"],
            "rejected_candidates": [],
            "confidence": 0.8,
        },
        "customer_context": {"customer_unique_id": "customer-a", "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [REF],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["INVESTIGATE_MISSING_EVIDENCE"],
        "claim_assessments": [
            {
                "claim_id": "claim-a",
                "verdict": "insufficient_evidence",
                "confidence": 0.2,
                "evidence_refs": [REF],
            }
        ],
    }


@pytest.fixture
def contracts():
    return Contracts(ROOT / "contracts" / "schemas")


class FakeGateway:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def discover_tools(self):
        return [TOOL]

    async def call(self, name, **arguments):
        self.calls.append((name, arguments))
        if self.fail:
            raise TimeoutError("simulated")
        return copy.deepcopy(EVIDENCE)


class FakeModel:
    def __init__(self, replies):
        self.replies = iter(replies)

    async def complete(self, system, payload, schema=None):
        return copy.deepcopy(next(self.replies))


def call(order_id="order-a", **extra):
    return {"calls": [{"tool": "get_order", "arguments": {"order_id": order_id, **extra}}]}


DONE = {"done": True, "findings": []}


def test_full_workflow_emits_real_handoffs_and_verifies(tmp_path, contracts):
    gateway = FakeGateway()
    model = FakeModel([call(), DONE, DONE, DONE, DONE, DONE, output(), {"approved": True}])
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    result = asyncio.run(solve_case(CASE, gateway, trace, model=model))
    assert result == output()
    assert gateway.calls == [("get_order", {"order_id": "order-a", "case_id": "CASE_001"})]
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert events[-1]["event_type"] == "verification_completed"
    assert len([e for e in events if e["event_type"] == "handoff"]) == 6


def test_cache_and_cross_case_guard(tmp_path, contracts):
    gateway = FakeGateway()
    model = FakeModel([call(case_id="CASE_999"), call(), call(), DONE])
    run = Investigation(CASE, gateway, TraceWriter(tmp_path / "trace", contracts), model)
    asyncio.run(run.specialist("entity-agent", [TOOL]))
    assert len(gateway.calls) == 1
    assert run.calls == 1


def test_role_permissions_reject_payment_tool_for_entity(tmp_path, contracts):
    payment = {**TOOL, "name": "get_payment"}
    request = {"calls": [{"tool": "get_payment", "arguments": {"order_id": "order-a"}}]}
    gateway = FakeGateway()
    run = Investigation(
        CASE, gateway, TraceWriter(tmp_path / "trace", contracts), FakeModel([request, DONE])
    )
    asyncio.run(run.specialist("entity-agent", [payment]))
    assert gateway.calls == []


def test_timeout_retry_is_bounded(tmp_path, contracts):
    gateway = FakeGateway(fail=True)
    run = Investigation(
        CASE, gateway, TraceWriter(tmp_path / "trace", contracts), FakeModel([call()] * 5)
    )
    asyncio.run(run.specialist("entity-agent", [TOOL]))
    assert len(gateway.calls) == 2
    assert run.evidence == []


def test_missing_evidence_does_not_produce_submission(tmp_path, contracts):
    with pytest.raises(RuntimeError, match="No MCP evidence"):
        asyncio.run(
            solve_case(
                CASE,
                FakeGateway(),
                TraceWriter(tmp_path / "trace", contracts),
                model=FakeModel([DONE] * 5),
            )
        )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda o: o.update(case_id="CASE_999"), "case_id mismatch"),
        (lambda o: o.update(evidence_refs=["ev_" + "b" * 24]), "unobserved"),
        (lambda o: o.update(claim_assessments=[]), "Each input claim"),
        (lambda o: o["entity_resolution"].update(rejected_candidates=["order-a"]), "overlap"),
        (lambda o: o["affected_entities"].update(item_ids=["imaginary"]), "absent from evidence"),
        (
            lambda o: o["payment_analysis"].update(
                captured_total_brl=100, refunded_total_brl=60, refundable_total_brl=50
            ),
            "remaining capture",
        ),
        (lambda o: o["financial_resolution"].update(recommended_refund_brl=10), "do not add up"),
    ],
)
def test_verifier_rejects_invalid_conclusions(contracts, mutation, match):
    value = output()
    mutation(value)
    with pytest.raises(ValueError, match=match):
        verify_output(value, CASE, [EVIDENCE], contracts)


def test_decimal_refund_arithmetic_and_policy(contracts):
    value = output()
    value["payment_analysis"].update(
        captured_total_brl=0.3, refunded_total_brl=0, refundable_total_brl=0.3
    )
    value["financial_resolution"].update(
        recommended_refund_brl=0.3,
        refund_lines=[
            {"reason_code": "REFUND", "amount_brl": n, "entity_id": "order-a"} for n in (0.1, 0.2)
        ],
    )
    with pytest.raises(ValueError, match="policy evidence"):
        verify_output(value, CASE, [EVIDENCE], contracts)
    policy = {**EVIDENCE, "domain": "policy", "evidence_ref": "ev_" + "c" * 24}
    value["evidence_refs"].append(policy["evidence_ref"])
    verify_output(value, CASE, [EVIDENCE, policy], contracts)


def test_verifier_cannot_approve_locally_invalid_output(tmp_path, contracts):
    invalid = output()
    invalid["evidence_refs"] = ["ev_" + "x" * 24]
    replies = [call(), *([DONE] * 5), invalid, *([{"approved": True}] * 3)]
    with pytest.raises(ValueError, match="Independent verification failed"):
        asyncio.run(
            solve_case(
                CASE,
                FakeGateway(),
                TraceWriter(tmp_path / "trace", contracts),
                model=FakeModel(replies),
            )
        )


def test_prepare_inputs_validates_before_writing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "CASE_001.json"
    path.write_text(json.dumps({"case_id": "CASE_002"}))
    with pytest.raises(ValueError, match="Invalid case ID"):
        prepare_inputs(tmp_path, source, "test-v1", expected_count=1)
    assert not (tmp_path / "case-set.json").exists()
    path.write_text(json.dumps(CASE))
    prepare_inputs(tmp_path, source, "test-v1", expected_count=1)
    assert load_case_set(tmp_path, expected_count=1).case_ids == ("CASE_001",)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        prepare_inputs(tmp_path, source, "test-v1", expected_count=1)


def test_packaging_requires_consumption_and_verification(tmp_path, contracts):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs/CASE_001.json").write_text(json.dumps(output()))
    trace = TraceWriter(tmp_path / "traces/trace.jsonl", contracts)
    cases = CaseSet("test-v1", "l3b", ("CASE_001",), {"CASE_001": CASE})
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    with pytest.raises(ValueError, match="missing from its case trace"):
        validate_artifacts(tmp_path, cases, contracts)
    trace.emit(
        case_id="CASE_001",
        event_type="tool_result_consumed",
        actor="entity-agent",
        tool_name="get_order",
        evidence_refs=[REF],
    )
    with pytest.raises(ValueError, match="missing successful verification"):
        validate_artifacts(tmp_path, cases, contracts)
    trace.emit(
        case_id="CASE_001",
        event_type="verification_completed",
        actor="verifier",
        decision_code="PASS",
    )
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")
    outputs, _ = validate_artifacts(tmp_path, cases, contracts)
    assert outputs["CASE_001"] == output()
