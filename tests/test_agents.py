from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.llm_agents import LLMAgents
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from test_workflow import FakeGateway, base_case, contracts, fake_evidence  # noqa: F401

ORDER = "af0bbb47f125381ce9f3597dc70ef07b"

class FakeResponse:
    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": self._message}]}

class FakeClient:
    """Scripted OpenAI: per role, a list of replies (tool calls or final JSON)."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {role: list(replies) for role, replies in script.items()}
        self.bodies: list[dict[str, Any]] = []

    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> FakeResponse:
        self.bodies.append(json)
        system = json["messages"][0]["content"]
        role = next(r for r in self.script if f"the {r.split('-')[0]}" in system)
        reply = self.script[role].pop(0) if self.script[role] else {"issue": None}
        if isinstance(reply, list):  # tool calls
            calls = [
                {"id": f"c{i}", "function": {"name": n, "arguments": _json(a)}}
                for i, (n, a) in enumerate(reply)
            ]
            return FakeResponse({"content": None, "tool_calls": calls})
        return FakeResponse({"content": _json(reply)})

def _json(value: Any) -> str:
    return json.dumps(value)

def _run(tmp_path: Path, contracts, case, evidence, script):  # noqa: F811
    trace_path = tmp_path / "trace.jsonl"
    gateway = FakeGateway(evidence)
    client = FakeClient(script)
    agents = LLMAgents("sk-test", "gpt-4o-mini", client=client)
    output = asyncio.run(solve_case(case, gateway, TraceWriter(trace_path, contracts), agents))
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    return output, gateway, client, events

def _script(coordinator: list[Any]) -> dict[str, list[Any]]:
    return {
        "entity-agent": [
            [("get_customer_history", {"customer_unique_id": "evil"})],
            [("get_order", {"order_id": "candidate-001"}), ("get_order", {"order_id": ORDER})],
            {"resolved_order_id": ORDER, "confidence": 0.9},
        ],
        "order-agent": [[("get_order_items", {"order_id": "x"})], {"issue": None}],
        "shipment-agent": [
            [("get_shipment_summary", {"order_id": "other"}), ("get_policy", {})],
            {"issue": "late_delivery_logistics", "confidence": 0.8},
        ],
        "payment-agent": [{"issue": None}],
        "policy-agent": [[("get_policy", {})], {"issue": None}],
        "coordinator": coordinator,
    }

def test_llm_agreement(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811
    output, gateway, client, events = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script([{"primary_issue": "late_delivery_logistics", "confidence": 0.9}]),
    )
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] == 0.9
    # guardrails: forced identity, no candidate ids, pinned order, no cross-role tools
    for name, args in gateway.calls:
        assert args["case_id"] == base_case["case_id"]
        if name == "get_customer_history":
            assert args["customer_unique_id"] == base_case["customer_unique_id_hint"]
        if "order_id" in args:
            assert args["order_id"] == ORDER
    names = [n for n, _ in gateway.calls]
    assert len(names) == len(set(names)) <= 9
    assert names.count("get_policy") == 1
    # case_id never offered to the LLM
    for body in client.bodies:
        for tool in body.get("tools", []):
            assert "case_id" not in tool["function"]["parameters"]["properties"]
    refs = set(output["evidence_refs"])
    consumed = {r for e in events for r in e.get("evidence_refs", [])}
    assert refs <= consumed
    assert len({e["actor"] for e in events}) >= 5
    verdicts = [e["decision_code"] for e in events if e["event_type"] == "verification_completed"]
    assert verdicts == ["PASS"]
    # one LLM call per specialist (3, run in parallel) + coordinator; no LLM tool loops
    assert len(client.bodies) == 4
    assert all("tools" not in body for body in client.bodies)
    assert "get_product_context" not in names

def test_llm_decision_is_final(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811
    output, _, _, events = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script([{"primary_issue": "duplicate_charge"}, {"primary_issue": "duplicate_charge"}]),
    )
    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    verdicts = [e["decision_code"] for e in events if e["event_type"] == "verification_completed"]
    assert verdicts[0] == "ISSUE_MISMATCH"
    assert verdicts[-1] in ("PASS_LLM_OVER_RULES",) or verdicts[-1].startswith("FAIL_")

def test_llm_revision_accepted(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811
    output, _, _, _ = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script(
            [{"primary_issue": "duplicate_charge"}, {"primary_issue": "late_delivery_logistics"}]
        ),
    )
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"

def test_llm_down_gives_insufficient(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811
    class Broken:
        async def post(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("openai down")

    trace_path = tmp_path / "trace.jsonl"
    gateway = FakeGateway(fake_evidence)
    agents = LLMAgents("sk-test", "gpt-4o-mini", client=Broken())
    output = asyncio.run(
        solve_case(base_case, gateway, TraceWriter(trace_path, contracts), agents)
    )
    # no LLM decision -> never silently use the rules' answer
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert "get_shipment_summary" in [n for n, _ in gateway.calls]
