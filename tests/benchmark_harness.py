"""Multi-dimensional benchmark and evaluation harness for Day09 L3B Multi-Agent system."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.cases import load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class BenchmarkHarness:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.settings = Settings.load(self.root)
        self.contracts = Contracts(self.root / "contracts" / "schemas")
        self.case_set = load_case_set(self.root)

    def score_case(
        self,
        case: dict[str, Any],
        output: dict[str, Any],
        trace_events: list[dict[str, Any]],
        audited_refs: set[str],
    ) -> dict[str, float]:
        scores: dict[str, float] = {}

        # 1. Schema Conformance (5%)
        try:
            self.contracts.validate_output(output, "eval_output")
            scores["schema"] = 1.0
        except Exception:
            scores["schema"] = 0.0
            zero_keys = [
                "schema",
                "semantic",
                "evidence",
                "provenance",
                "consistency",
                "calibration",
                "workflow",
                "efficiency",
                "composite",
            ]
            return {k: 0.0 for k in zero_keys}

        # 2. Provenance Audit (15%)
        cited_refs = set(output.get("evidence_refs", []))
        for ca in output.get("claim_assessments", []):
            cited_refs.update(ca.get("evidence_refs", []))

        if cited_refs and cited_refs.issubset(audited_refs):
            scores["provenance"] = 1.0
        elif not cited_refs:
            scores["provenance"] = 0.0
        else:
            scores["provenance"] = len(cited_refs & audited_refs) / len(cited_refs)

        # 3. Semantic Correctness (40%)
        er = output.get("entity_resolution", {})
        cand_resolved = bool(er.get("resolved_order_ids") and er.get("status") == "resolved")
        cands_rejected = bool(
            er.get("rejected_candidates") and "candidate-" in er["rejected_candidates"][0]
        )

        assessment = output.get("assessment", {})
        primary_issue = assessment.get("primary_issue")
        claims = case.get("customer_request", {}).get("claims", [])
        claimed_topics = [c.get("topic") for c in claims]
        expected_topic = claimed_topics[0]

        semantic_score = 0.0
        if primary_issue == expected_topic or (
            primary_issue in ("late_delivery_seller", "late_delivery_logistics")
            and "delivery" in expected_topic
        ):
            semantic_score += 0.5
        else:
            semantic_score += 0.1

        if cand_resolved and cands_rejected:
            semantic_score += 0.25

        cas = output.get("claim_assessments", [])
        if len(cas) == len(claimed_topics):
            semantic_score += 0.25

        scores["semantic"] = min(1.0, semantic_score)

        # 4. Evidence Quality (15%)
        has_refs = len(output.get("evidence_refs", [])) >= 4
        all_claims_have_refs = all(bool(c.get("evidence_refs")) for c in cas)
        scores["evidence"] = 1.0 if (has_refs and all_claims_have_refs) else 0.5

        # 5. Consistency Invariants (10%)
        fin = output.get("financial_resolution", {})
        pay = output.get("payment_analysis", {})
        rec_refund = fin.get("recommended_refund_brl", 0.0)
        captured = pay.get("captured_total_brl", 0.0) or 0.0
        math_ok = rec_refund <= captured

        status = assessment.get("case_status")
        status_ok = True
        if rec_refund > 0 and status != "action_required":
            status_ok = False
        if rec_refund == 0 and status not in ("no_action", "needs_investigation"):
            status_ok = False

        scores["consistency"] = 1.0 if (math_ok and status_ok) else 0.5

        # 6. Confidence Calibration (5%)
        conf = assessment.get("confidence", 0.5)
        if 0.7 <= conf <= 0.98:
            scores["calibration"] = 1.0
        elif 0.5 <= conf <= 1.0:
            scores["calibration"] = 0.7
        else:
            scores["calibration"] = 0.3

        # 7. Workflow Adherence (5%)
        types_in_trace = [e.get("event_type") for e in trace_events]
        req = [
            "case_received",
            "task_assigned",
            "handoff",
            "verification_completed",
            "case_finalized",
        ]
        req_present = all(r in types_in_trace for r in req)
        tool_consumed = "tool_result_consumed" in types_in_trace
        scores["workflow"] = 1.0 if (req_present and tool_consumed) else 0.5

        # 8. Tool Efficiency (5%)
        total_calls = len(audited_refs)
        cited_count = len(output.get("evidence_refs", []))
        if total_calls > 0:
            eff = min(1.0, cited_count / total_calls)
            scores["efficiency"] = round(eff, 2)
        else:
            scores["efficiency"] = 0.5

        # Composite score
        composite = (
            scores["semantic"] * 40.0
            + scores["evidence"] * 15.0
            + scores["provenance"] * 15.0
            + scores["consistency"] * 10.0
            + scores["schema"] * 5.0
            + scores["calibration"] * 5.0
            + scores["workflow"] * 5.0
            + scores["efficiency"] * 5.0
        )
        scores["composite"] = round(composite, 2)
        return scores

    async def run_benchmark(self, case_ids: list[str]) -> dict[str, Any]:
        trace_path = self.root / "traces" / "benchmark_trace.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.unlink(missing_ok=True)
        trace = TraceWriter(trace_path, self.contracts)

        results: dict[str, Any] = {}
        async with connect_gateway(
            self.settings.mcp_endpoint, self.settings.team_api_key, self.contracts
        ) as gateway:
            orig_call = gateway.call
            for case_id in case_ids:
                case = self.case_set.cases[case_id]
                trace_start_len = 0
                if trace_path.exists():
                    trace_start_len = len(trace_path.read_text().splitlines())

                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")

                audited_refs: set[str] = set()

                def make_tracker(refs_set: set[str]):
                    async def tracking_call(tool_name: str, **kwargs: Any):
                        ev = await orig_call(tool_name, **kwargs)
                        if ev and "evidence_ref" in ev:
                            refs_set.add(ev["evidence_ref"])
                        return ev

                    return tracking_call

                gateway.call = make_tracker(audited_refs)

                output = await solve_case(case, gateway, trace)
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")

                trace_lines = trace_path.read_text().splitlines()[trace_start_len:]
                events = [json.loads(line) for line in trace_lines]

                case_scores = self.score_case(case, output, events, audited_refs)
                results[case_id] = {
                    "scores": case_scores,
                    "primary_issue": output["assessment"]["primary_issue"],
                    "case_status": output["assessment"]["case_status"],
                    "refund_brl": output["financial_resolution"]["recommended_refund_brl"],
                    "evidence_count": len(output["evidence_refs"]),
                }

        # Calculate averages
        avg_scores = {}
        metrics = [
            "semantic",
            "evidence",
            "provenance",
            "consistency",
            "schema",
            "calibration",
            "workflow",
            "efficiency",
            "composite",
        ]
        for metric in metrics:
            values = [r["scores"][metric] for r in results.values()]
            avg_scores[metric] = round(sum(values) / len(values), 3)

        return {"case_results": results, "averages": avg_scores}


async def main():
    root = Path(".")
    harness = BenchmarkHarness(root)
    test_cases = [f"L3B_CASE_{i:03d}" for i in range(1, 11)]
    print(f"Running benchmark on {len(test_cases)} representative cases...")
    report = await harness.run_benchmark(test_cases)
    print("\n================ BENCHMARK RESULTS ================")
    for cid, r in report["case_results"].items():
        sc = r["scores"]
        comp = sc["composite"]
        issue = r["primary_issue"]
        print(f"{cid}: Composite={comp} -> Issue={issue}")
    print("\n================ COMPONENT AVERAGES ================")
    for k, v in report["averages"].items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
