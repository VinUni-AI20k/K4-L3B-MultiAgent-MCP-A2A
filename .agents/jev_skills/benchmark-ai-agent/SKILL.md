---
name: benchmark-ai-agent
description: Use when evaluating, testing, or benchmarking AI agent performance against multi-case datasets, measuring semantic accuracy, evidence provenance, JSON schema conformance, consistency invariants, calibration (ECE/Brier score), workflow adherence, and tool efficiency.
---

# Benchmark AI Agent: Multi-Dimensional Evaluation Guide

Engineering guide for establishing rigorous benchmarking, scoring metrics, and failure diagnostics for multi-agent systems and LLM investigation pipelines.

## When to Use This Skill

- Running benchmark suites against multi-case datasets (e.g., 50–100 realistic e-commerce disputes).
- Evaluating multi-agent performance across multi-dimensional rubrics (accuracy, evidence, consistency, efficiency).
- Measuring confidence calibration (Expected Calibration Error, Brier score) to prevent overconfident hallucinations.
- Diagnosing agent failure modes: schema errors, cross-case data leakage, circular handoffs, and excessive tool fan-out.
- Building automated regression harnesses and submission packagers for AI agent competitions.

---

## 1. The Multi-Dimensional Scoring Rubric

High-reliability agents are evaluated across eight distinct dimensions rather than simple output accuracy:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        AI Agent Benchmark Matrix                       │
├───────────────────────────────┬────────┬───────────────────────────────┤
│ Dimension                     │ Weight │ Verification Method           │
├───────────────────────────────┼────────┼───────────────────────────────┤
│ 1. Semantic Correctness       │   40%  │ Business outcome vs ground    │
│                               │        │ truth (refund/rejection/party)│
│ 2. Evidence Quality           │   15%  │ Grounding in verified facts   │
│ 3. Provenance Audit           │   15%  │ Evidence refs exist in audit  │
│ 4. Internal Consistency       │   10%  │ Timelines, math, claim links  │
│ 5. Schema Conformance         │    5%  │ Strict JSON Schema validation │
│ 6. Confidence Calibration     │    5%  │ Brier score / ECE alignment   │
│ 7. Multi-Agent Workflow       │    5%  │ Structured trace events       │
│ 8. Tool Efficiency            │    5%  │ Useful vs total tool calls    │
└───────────────────────────────┴────────┴───────────────────────────────┘
```

### Critical Zero-Score Invariants (Immediate Disqualification)
A case score drops to **0** if any of the following occur:
1. **Schema Non-Conformance**: Output fails target JSON schema validation.
2. **Missing Mandatory Evidence**: Final claim has no linked `evidence_ref`.
3. **Manufactured Evidence**: `evidence_ref` was not issued by the server in that specific case run.
4. **Cross-Case Leakage**: Evidence from case `A` is cited in case `B`.

---

## 2. Confidence Calibration & Reliability

Overconfident agents are penalized heavily. Calibrate the output confidence score $c \in [0.0, 1.0]$:

```python
def compute_calibrated_confidence(
    entity_resolved: bool,
    evidence_completeness: float,  # 0.0 to 1.0
    conflicts_unresolved: bool,
) -> float:
    """Calibrates confidence based on factual grounding rather than LLM intuition."""
    if not entity_resolved:
        return 0.25  # Ambiguous entity -> low confidence
    if conflicts_unresolved:
        return 0.40  # Conflicting evidence -> medium-low
    if evidence_completeness < 0.7:
        return 0.65  # Incomplete evidence
    return 0.95      # Unambiguous entity with full documentary proof
```

### Expected Calibration Error (ECE) Formula
$$\text{ECE} = \sum_{m=1}^{M} \frac{|B_m|}{N} \left| \text{acc}(B_m) - \text{conf}(B_m) \right|$$

---

## 3. Tool Efficiency & Query Budgeting

The **Efficiency Metric** penalizes unguided tool exploration:

$$\text{Efficiency Score} = \frac{\text{Consumed Unique Tool Calls}}{\text{Total Tool Calls Made}}$$

### Best Practices for Maximizing Efficiency:
1. **Targeted Investigation**: Inspect the complaint before querying tools. If the dispute is about delivery delays, do not query payment installment details.
2. **Case-Level Memoization**: Cache identical queries within the same case context.
3. **Bounded Retries**: Maximum 2 retries with exponential backoff on network failures. Never retry 404/Not Found queries.

---

## 4. Benchmark Harness Implementation

```python
import json
from pathlib import Path
from jsonschema import Draft202012Validator

class AgentBenchmarkRunner:
    def __init__(self, schema_path: Path, ground_truth_path: Path):
        with schema_path.open() as f:
            self.validator = Draft202012Validator(json.load(f))
        with ground_truth_path.open() as f:
            self.ground_truth = json.load(f)

    def evaluate_output(self, case_id: str, output: dict, trace_events: list[dict], audit_calls: set[str]) -> dict[str, float]:
        scores = {}
        
        # 1. Schema check
        errors = list(self.validator.iter_errors(output))
        scores["schema"] = 1.0 if not errors else 0.0
        if not scores["schema"]:
            return {k: 0.0 for k in ["schema", "semantic", "evidence", "provenance", "consistency", "calibration", "workflow", "efficiency"]}

        # 2. Provenance Audit check
        cited_refs = set(output.get("evidence_refs", []))
        provenance_valid = cited_refs.issubset(audit_calls)
        scores["provenance"] = 1.0 if provenance_valid else 0.0

        # 3. Semantic match
        expected = self.ground_truth.get(case_id, {})
        outcome_match = (output.get("decision") == expected.get("decision"))
        scores["semantic"] = 1.0 if outcome_match else 0.0

        # 4. Consistency invariants
        timeline_ok = output.get("delivered_at", "") >= output.get("shipped_at", "")
        financials_ok = output.get("refund_amount", 0) <= output.get("order_total", 0)
        scores["consistency"] = 1.0 if (timeline_ok and financials_ok) else 0.5

        # 5. Workflow adherence in trace
        has_task_assigned = any(e.get("event_type") == "task_assigned" for e in trace_events)
        has_tool_consumed = any(e.get("event_type") == "tool_result_consumed" for e in trace_events)
        has_verification = any(e.get("event_type") == "verification_completed" for e in trace_events)
        scores["workflow"] = 1.0 if (has_task_assigned and has_tool_consumed and has_verification) else 0.5

        # 6. Efficiency
        total_calls = len(audit_calls)
        consumed_calls = len(cited_refs)
        scores["efficiency"] = (consumed_calls / total_calls) if total_calls > 0 else 0.0

        return scores
```

---

## 5. Diagnostic Workflow for Low Scores

When benchmark scores drop:
1. **Filter Schema Failures**: Run `day09 validate` to verify field types and required properties.
2. **Audit Check**: Check if any `evidence_ref` in `outputs/<case_id>.json` was missed in `traces/trace.jsonl`.
3. **Disambiguation Review**: For cases with candidate entities, check if the entity resolver picked the highest-probability candidate.
4. **Conflict Table**: Verify that opposing claims between buyer and seller are recorded in the conflict log before the final decision is reached.
