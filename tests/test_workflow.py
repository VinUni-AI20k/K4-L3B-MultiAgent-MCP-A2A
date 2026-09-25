from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    def __init__(self, responses: dict[tuple[str, str], dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    async def describe_tools(self) -> list[dict]:
        return [
            {
                "name": name,
                "input_schema": {
                    "type": "object",
                    "required": ["case_id", "order_id"],
                    "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
                },
            }
            for name in sorted({name for name, _ in self.responses})
        ]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
        self.calls.append((tool_name, case_id, arguments))
        return self.responses[(tool_name, arguments["order_id"])]


def evidence(domain: str, suffix: str, data: dict) -> dict:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_" + suffix * 22,
        "result_hash": "sha256:" + "a" * 64,
        "domain": domain,
        "data": data,
    }


def run_case(
    tmp_path: Path, gateway: FakeGateway, case_overrides: dict | None = None
) -> tuple[dict, list[dict]]:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "order-good",
            "claims": [{"claim_id": "claim-1", "topic": "duplicate_charge"}],
        },
        "candidate_order_ids": ["order-good", "order-other"],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {"include_customer_history": True},
    }
    case.update(case_overrides or {})
    output = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(output, "test output")
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    return output, events


def test_workflow_resolves_candidate_and_keeps_evidence_scoped(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order", "a", {"order_id": "order-good", "customer_unique_id": "customer-1"}
            ),
            ("get_order", "order-other"): evidence(
                "order", "b", {"order_id": "order-other", "customer_unique_id": "someone-else"}
            ),
            ("get_payment", "order-good"): evidence(
                "payment", "c", {"order_id": "order-good", "captured_total_brl": 100}
            ),
            ("get_payment", "order-other"): evidence(
                "payment", "d", {"order_id": "order-other", "captured_total_brl": 500}
            ),
        }
    )
    output, events = run_case(tmp_path, gateway)
    assert output["entity_resolution"]["resolved_order_ids"] == ["order-good"]
    assert output["entity_resolution"]["rejected_candidates"] == []
    assert not any(
        name == "get_order" and args["order_id"] == "order-other"
        for name, _, args in gateway.calls
    )
    assert output["payment_analysis"]["captured_total_brl"] == 100
    assert "ev_" + "d" * 22 not in output["evidence_refs"]
    assert all(case_id == "L3B_CASE_001" for _, case_id, _ in gateway.calls)
    assert {event["event_type"] for event in events} >= {
        "task_assigned",
        "handoff",
        "tool_result_consumed",
        "verification_completed",
    }


def test_workflow_stops_on_first_mcp_tool_error(tmp_path: Path) -> None:
    class FailingGateway(FakeGateway):
        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
            self.calls.append((tool_name, case_id, arguments))
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")

    gateway = FailingGateway({("get_order", "order-good"): {}})
    with pytest.raises(RuntimeError, match="MCP tool get_order failed"):
        run_case(tmp_path, gateway)
    assert len(gateway.calls) == 1


def test_validation_rejects_output_ref_without_matching_trace_event(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order", "a", {"order_id": "order-good", "customer_unique_id": "customer-1"}
            )
        }
    )
    output, events = run_case(tmp_path, gateway)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    (output_root / "L3B_CASE_001.json").write_text(json.dumps(output), encoding="utf-8")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace_path.parent.mkdir()
    trace_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in events
            if event["event_type"] != "tool_result_consumed"
        ),
        encoding="utf-8",
    )
    cases = CaseSet("test-v1", "l3b", ("L3B_CASE_001",), {})
    with pytest.raises(ValueError, match="evidence ref missing from trace"):
        validate_artifacts(tmp_path, cases, Contracts(ROOT / "contracts" / "schemas"))


def test_validation_rejects_run_with_no_mcp_evidence(tmp_path: Path) -> None:
    output, events = run_case(tmp_path, FakeGateway({}))
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    (output_root / "L3B_CASE_001.json").write_text(json.dumps(output), encoding="utf-8")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace_path.parent.mkdir()
    trace_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    cases = CaseSet("test-v1", "l3b", ("L3B_CASE_001",), {})
    with pytest.raises(ValueError, match="no MCP evidence"):
        validate_artifacts(tmp_path, cases, Contracts(ROOT / "contracts" / "schemas"))


def test_workflow_does_not_invent_resolution_without_tools(tmp_path: Path) -> None:
    output, _ = run_case(tmp_path, FakeGateway({}))
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["evidence_refs"] == []
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_entity_resolution_does_not_call_unrelated_order_tool(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order_items", "order-good"): evidence(
                "item", "a", {"order_id": "order-good", "item_ids": ["item-1"]}
            )
        }
    )
    output, _ = run_case(tmp_path, gateway)
    assert output["entity_resolution"]["status"] == "not_found"
    assert gateway.calls == []


def test_workflow_rejects_wrong_case_evidence(tmp_path: Path) -> None:
    response = evidence("order", "a", {"order_id": "order-good", "case_id": "OTHER_CASE"})
    output, events = run_case(tmp_path, FakeGateway({("get_order", "order-good"): response}))
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["evidence_refs"] == []
    assert not any(event["event_type"] == "tool_result_consumed" for event in events)


def test_workflow_reports_equal_candidates_as_ambiguous(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence("order", "a", {"order_id": "order-good"}),
            ("get_order", "order-other"): evidence("order", "b", {"order_id": "order-other"}),
        }
    )
    output, _ = run_case(
        tmp_path, gateway, {"customer_request": {"claims": []}, "customer_unique_id_hint": None}
    )
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["entity_resolution"]["resolved_order_ids"] == []
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_workflow_discards_payment_for_other_order(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order", "a", {"order_id": "order-good", "customer_unique_id": "customer-1"}
            ),
            ("get_payment", "order-good"): evidence(
                "payment", "c", {"order_id": "order-other", "captured_total_brl": 1000}
            ),
        }
    )
    output, _ = run_case(tmp_path, gateway)
    assert output["payment_analysis"]["captured_total_brl"] is None
    assert "ev_" + "c" * 22 not in output["evidence_refs"]


def test_workflow_uses_model_only_to_choose_evidenced_issue(tmp_path: Path) -> None:
    class ChoosingModel:
        async def select_issue(
            self, case: dict, candidates: list[str], evidence: list[dict]
        ) -> str:
            assert set(candidates) == {"duplicate_charge", "late_delivery_logistics"}
            return "late_delivery_logistics"

    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order", "a", {"order_id": "order-good", "customer_unique_id": "customer-1"}
            ),
            ("get_payment", "order-good"): evidence(
                "payment", "b", {"order_id": "order-good", "issue_code": "duplicate_charge"}
            ),
            ("get_shipment", "order-good"): evidence(
                "shipment", "c", {"order_id": "order-good", "verdict": "logistics_delay"}
            ),
        }
    )
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "L3B_CASE_001",
        "customer_request": {"claimed_order_id": "order-good"},
        "candidate_order_ids": ["order-good"],
        "customer_unique_id_hint": "customer-1",
    }
    output = asyncio.run(solve_case(case, gateway, trace, model=ChoosingModel()))
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["secondary_issues"] == ["duplicate_charge"]


def test_workflow_reads_payment_timeline_and_sellers(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order", "a", {"order_id": "order-good", "customer_unique_id": "customer-1"}
            ),
            ("get_order_payments", "order-good"): evidence(
                "payment", "b", {"order_id": "order-good", "captured_total_brl": 100}
            ),
            ("get_payment_timeline", "order-good"): evidence(
                "payment", "c", {"order_id": "order-good", "issue_code": "duplicate_charge"}
            ),
            ("get_sellers", "order-good"): evidence(
                "seller", "d", {"order_id": "order-good", "seller_ids": ["seller-1"]}
            ),
        }
    )
    output, _ = run_case(tmp_path, gateway)
    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    assert output["payment_analysis"]["captured_total_brl"] == 100
    assert output["affected_entities"]["seller_ids"] == ["seller-1"]
    assert "ev_" + "c" * 22 in output["evidence_refs"]


def test_workflow_collects_ids_from_all_evidence_rows(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order", "a", {"order_id": "order-good", "customer_unique_id": "customer-1"}
            ),
            ("get_order_items", "order-good"): evidence(
                "item",
                "b",
                {
                    "order_id": "order-good",
                    "items": [
                        {"item_id": "item-1", "seller_id": "seller-1"},
                        {"item_id": "item-2", "seller_id": "seller-2"},
                    ],
                },
            ),
        }
    )
    output, _ = run_case(tmp_path, gateway)
    assert output["affected_entities"]["item_ids"] == ["item-1", "item-2"]
    assert output["affected_entities"]["seller_ids"] == ["seller-1", "seller-2"]


def test_workflow_can_resolve_conflicting_evidence_without_local_runtime(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order", "a", {"order_id": "order-good", "customer_unique_id": "customer-1"}
            ),
            ("get_payment", "order-good"): evidence(
                "payment", "b", {"order_id": "order-good", "issue_code": "duplicate_charge"}
            ),
            ("get_shipment", "order-good"): evidence(
                "shipment", "c", {"order_id": "order-good", "verdict": "logistics_delay"}
            ),
        }
    )
    output, _ = run_case(tmp_path, gateway)
    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    claim = output["claim_assessments"][0]
    assert claim["verdict"] == "supported"
    assert "ev_" + "b" * 22 in claim["evidence_refs"]
    assert "ev_" + "c" * 22 not in claim["evidence_refs"]


def test_claim_does_not_cite_shipment_that_disagrees(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("get_order", "order-good"): evidence(
                "order",
                "a",
                {
                    "order_id": "order-good",
                    "customer_unique_id": "customer-1",
                    "issue_code": "late_delivery_logistics",
                },
            ),
            ("get_shipment", "order-good"): evidence(
                "shipment", "b", {"order_id": "order-good", "verdict": "on_time"}
            ),
        }
    )
    output, _ = run_case(
        tmp_path,
        gateway,
        {
            "customer_request": {
                "claimed_order_id": "order-good",
                "claims": [{"claim_id": "claim-1", "topic": "late_delivery_logistics"}],
            }
        },
    )
    claim_refs = output["claim_assessments"][0]["evidence_refs"]
    assert "ev_" + "a" * 22 in claim_refs
    assert "ev_" + "b" * 22 not in claim_refs
