#!/usr/bin/env python3
"""Offline audit of observable MCP evidence use in a Day09 L3B run.

The trace schema records ``tool_result_consumed`` events, not every attempted
MCP call. Consequently the counts below are observable consumed results, never
claims about the server's private call budget or complete attempt count.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACE = ROOT / "traces" / "trace.jsonl"
DEFAULT_OUTPUTS = ROOT / "outputs"
DEFAULT_INPUTS = ROOT / "inputs"
if not any(DEFAULT_INPUTS.glob("*.json")):
    DEFAULT_INPUTS = ROOT / "l3b-inputs-v1" / "inputs"

# These are code-path observations for the current solve_case implementation.
# They distinguish a domain that feeds substantive output fields from refs that
# are merely copied into the generic evidence_refs/claim_assessments lists.
TOOL_ROLE = {
    "get_order": "entity resolution and order identifiers",
    "get_customer_history": "customer context and purchase-time conflict resolution",
    "get_order_items": "item/seller identifiers and shipping/payment reconciliation",
    "get_shipment_summary": "shipment verdict and timeline completeness",
    "get_payment_timeline": "capture totals and payment reconciliation",
    "get_refund_timeline": "refund totals/status and refund-related decisions",
    "get_product_context": "no substantive output field currently consumes the returned payload",
    "get_policy": "case status, resolution action, and refund recommendation",
}

TOOL_SEMANTIC_CHECKS = {
    "get_order": lambda o: bool(_list_at(o, "entity_resolution", "resolved_order_ids")),
    "get_customer_history": lambda o: bool(
        _value_at(o, "customer_context", "customer_unique_id")
        or _list_at(o, "customer_context", "related_order_ids")
        or any(
            "get_customer_history" in x.get("sources", []) for x in _list_at(o, "data_conflicts")
        )
    ),
    "get_order_items": lambda o: bool(
        _list_at(o, "affected_entities", "item_ids")
        or _list_at(o, "affected_entities", "seller_ids")
        or _list_at(o, "affected_entities", "payment_references")
    ),
    "get_shipment_summary": lambda o: bool(
        _value_at(o, "shipment_analysis", "timeline_complete")
        or _value_at(o, "shipment_analysis", "verdict") not in (None, "insufficient_evidence")
    ),
    "get_payment_timeline": lambda o: any(
        _value_at(o, "payment_analysis", key) is not None
        for key in ("captured_total_brl", "refunded_total_brl", "refundable_total_brl")
    ),
    "get_refund_timeline": lambda o: (
        _value_at(o, "payment_analysis", "refunded_total_brl") is not None
        or _value_at(o, "payment_analysis", "verdict")
        in {"refund_pending", "refund_failed", "refunded"}
    ),
    "get_product_context": lambda o: False,
    "get_policy": lambda o: bool(
        _list_at(o, "resolution_actions")
        or _value_at(o, "financial_resolution", "recommended_refund_brl") is not None
        or _value_at(o, "assessment", "case_status")
    ),
}


def _value_at(obj: Any, *keys: str) -> Any:
    value = obj
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _list_at(obj: Any, *keys: str) -> list[Any]:
    value = _value_at(obj, *keys)
    return value if isinstance(value, list) else []


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_json_dir(path: Path, warnings: list[str], kind: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        warnings.append(f"{kind} directory not found: {path}")
        return records
    for file in sorted(path.glob("*.json")):
        try:
            value = _read_json(file)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"could not read {kind} {file.name}: {exc}")
            continue
        if not isinstance(value, dict):
            warnings.append(f"ignored non-object {kind}: {file.name}")
            continue
        case_id = value.get("case_id")
        if not isinstance(case_id, str):
            warnings.append(f"ignored {kind} without case_id: {file.name}")
            continue
        records[case_id] = value
    return records


def _load_trace(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        warnings.append(f"trace not found: {path}")
        return events
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append(f"invalid JSON on trace line {line_no}: {exc}")
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                warnings.append(f"ignored non-object trace event on line {line_no}")
    return events


def _refs(value: Any) -> set[str]:
    return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()


def _input_topic(case: dict[str, Any] | None) -> str:
    if not isinstance(case, dict):
        return "?"
    req = case.get("customer_request")
    claims = req.get("claims", []) if isinstance(req, dict) else []
    topics = (
        [x.get("topic") for x in claims if isinstance(x, dict) and isinstance(x.get("topic"), str)]
        if isinstance(claims, list)
        else []
    )
    return topics[0] if topics else "?"


def _planned_fetches(case: dict[str, Any] | None, output: dict[str, Any]) -> list[str]:
    """Mirror current workflow.py fetch call sites, not network attempts/retries."""
    if not isinstance(case, dict):
        return []
    req = case.get("customer_request", {})
    planned: list[str] = []
    if (
        isinstance(req, dict)
        and isinstance(req.get("claimed_order_id"), str)
        and req["claimed_order_id"]
    ):
        planned.append("get_order")
    planned.extend(["get_customer_history"])
    if _value_at(output, "entity_resolution", "status") == "resolved":
        planned.extend(
            [
                "get_order_items",
                "get_shipment_summary",
                "get_payment_timeline",
                "get_product_context",
            ]
        )
        if _input_topic(case) in {
            "payment_mismatch",
            "refund_failed",
            "refund_pending",
            "valid_split_payment",
        }:
            planned.append("get_refund_timeline")
    planned.append("get_policy")
    return planned


def audit(trace: Path, outputs_dir: Path, inputs_dir: Path) -> dict[str, Any]:
    warnings: list[str] = []
    events = _load_trace(trace, warnings)
    outputs = _load_json_dir(outputs_dir, warnings, "output")
    inputs = _load_json_dir(inputs_dir, warnings, "input")

    per_case_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        case_id = event.get("case_id")
        if isinstance(case_id, str):
            per_case_events[case_id].append(event)

    case_ids = sorted(set(per_case_events) | set(outputs) | set(inputs))
    case_rows: list[dict[str, Any]] = []
    tool_rows: dict[str, dict[str, Any]] = {}
    unassigned_output_refs: set[str] = set()
    missing_tool_name_events = 0
    planned_by_tool: Counter[str] = Counter()
    no_observed_result_by_tool: Counter[str] = Counter()

    for case_id in case_ids:
        case_events = per_case_events.get(case_id, [])
        tool_events = [e for e in case_events if e.get("event_type") == "tool_result_consumed"]
        calls_by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in tool_events:
            name = event.get("tool_name")
            if isinstance(name, str) and name:
                calls_by_tool[name].append(event)
            else:
                missing_tool_name_events += 1

        output = outputs.get(case_id, {})
        input_record = inputs.get(case_id)
        planned_fetches = _planned_fetches(input_record, output)
        planned_by_tool.update(planned_fetches)
        no_observed_result_by_tool.update(
            tool for tool in planned_fetches if tool not in calls_by_tool
        )
        output_refs = _refs(output.get("evidence_refs"))
        claims = output.get("claim_assessments", [])
        claim_refs: set[str] = set()
        if isinstance(claims, list):
            for claim in claims:
                if isinstance(claim, dict):
                    claim_refs |= _refs(claim.get("evidence_refs"))
        observable_refs: set[str] = set()
        listed_refs: set[str] = set()
        call_names = sorted(calls_by_tool)
        material_tools: list[str] = []
        no_signal_tools: list[str] = []
        for tool, tool_call_events in calls_by_tool.items():
            refs = set().union(*(_refs(e.get("evidence_refs")) for e in tool_call_events))
            observable_refs |= refs
            output_linked = refs & output_refs
            claim_linked = refs & claim_refs
            listed_refs |= output_linked | claim_linked
            predicate = TOOL_SEMANTIC_CHECKS.get(tool)
            semantic_signal = bool(predicate(output)) if predicate else False
            # A product result is known to have its payload discarded in the current
            # workflow. Other roles are domain-level signals, not causal proof.
            if semantic_signal and tool != "get_product_context":
                material_tools.append(tool)
            elif tool in TOOL_ROLE:
                no_signal_tools.append(tool)
            row = tool_rows.setdefault(
                tool,
                {
                    "tool_name": tool,
                    "observed_consumed_result_events": 0,
                    "cases_with_observed_result": 0,
                    "evidence_refs_emitted": 0,
                    "evidence_refs_listed_in_output": 0,
                    "evidence_refs_listed_in_claims": 0,
                    "cases_with_domain_output_signal": 0,
                    "cases_without_domain_output_signal": 0,
                    "role": TOOL_ROLE.get(tool, "unknown domain role"),
                },
            )
            row["observed_consumed_result_events"] += len(tool_call_events)
            row["cases_with_observed_result"] += 1
            row["evidence_refs_emitted"] += len(refs)
            row["evidence_refs_listed_in_output"] += len(refs & output_refs)
            row["evidence_refs_listed_in_claims"] += len(refs & claim_refs)
            row["cases_with_domain_output_signal"] += int(semantic_signal)
            row["cases_without_domain_output_signal"] += int(not semantic_signal)

        unassigned_output_refs |= output_refs - observable_refs
        case_rows.append(
            {
                "case_id": case_id,
                "input_topic": _input_topic(input_record),
                "trace_event_count": len(case_events),
                "observable_consumed_result_events": len(tool_events),
                "observable_tool_names": call_names,
                "workflow_planned_fetches": planned_fetches,
                "planned_fetches_without_observed_result": sorted(
                    tool for tool in set(planned_fetches) if tool not in calls_by_tool
                ),
                "evidence_refs_emitted": len(observable_refs),
                "evidence_refs_listed_in_final_output": len(output_refs & observable_refs),
                "evidence_refs_listed_in_claims": len(claim_refs & observable_refs),
                "generic_output_ref_coverage": round(
                    len(output_refs & observable_refs) / len(observable_refs), 4
                )
                if observable_refs
                else None,
                "tools_with_domain_output_signal": sorted(material_tools),
                "tools_without_domain_output_signal": sorted(no_signal_tools),
                "missing_output_or_input": not bool(output) or input_record is None,
            }
        )

    for tool in tool_rows:
        tool_rows[tool]["workflow_planned_fetch_invocations"] = planned_by_tool[tool]
        tool_rows[tool]["planned_fetches_without_observed_result"] = no_observed_result_by_tool[
            tool
        ]
        tool_rows[tool]["output_ref_link_rate"] = (
            round(
                tool_rows[tool]["evidence_refs_listed_in_output"]
                / tool_rows[tool]["evidence_refs_emitted"],
                4,
            )
            if tool_rows[tool]["evidence_refs_emitted"]
            else None
        )

    observed_tool_events = sum(row["observable_consumed_result_events"] for row in case_rows)
    observed_evidence_refs = sum(row["evidence_refs_emitted"] for row in tool_rows.values())
    output_referenced = sum(row["evidence_refs_listed_in_output"] for row in tool_rows.values())
    claim_referenced = sum(row["evidence_refs_listed_in_claims"] for row in tool_rows.values())
    topic_counts = Counter(row["input_topic"] for row in case_rows if row["input_topic"] != "?")
    refund_observed_by_topic = Counter()
    for row in case_rows:
        if "get_refund_timeline" in row["observable_tool_names"]:
            refund_observed_by_topic[row["input_topic"]] += 1

    recommendations: list[dict[str, str]] = []
    product = tool_rows.get("get_product_context")
    if product and product["observed_consumed_result_events"]:
        recommendations.append(
            {
                "priority": "high",
                "finding": (
                    "get_product_context payload is fetched, then discarded; only its evidence ref "
                    "is copied into generic output lists."
                ),
                "action": (
                    "Skip it when no product claim is in scope, or use its fields in an explicit "
                    "decision/output."
                ),
                "evidence": (
                    f"{product['observed_consumed_result_events']} observed results; "
                    "0 domain-output signals."
                ),
            }
        )
    refund = tool_rows.get("get_refund_timeline")
    if (
        refund
        and planned_by_tool["get_refund_timeline"]
        > refund["observed_consumed_result_events"]
    ):
        recommendations.append(
            {
                "priority": "review",
                "finding": (
                    "Some planned get_refund_timeline calls have no consumed result in the trace."
                ),
                "action": (
                    "Review those cases before adding further gates or retries; preserve refund "
                    "evidence needed for claim assessment."
                ),
                "evidence": (
                    f"{planned_by_tool['get_refund_timeline']} planned calls; "
                    f"{refund['observed_consumed_result_events']} consumed results; "
                    f"{no_observed_result_by_tool['get_refund_timeline']} lack a trace result."
                ),
            }
        )
    if output_referenced == observed_evidence_refs and observed_evidence_refs:
        recommendations.append(
            {
                "priority": "interpretation",
                "finding": (
                    "Every observed ref is listed in output and claims. Current code copies "
                    "all_refs "
                    "into each claim, so this is not proof of decision use."
                ),
                "action": (
                    "Track field-level provenance or domain-specific evidence subsets; do not use "
                    "generic ref coverage as a quality score."
                ),
                "evidence": (
                    f"{output_referenced}/{observed_evidence_refs} refs listed in output; "
                    f"{claim_referenced}/{observed_evidence_refs} listed in claims."
                ),
            }
        )

    return {
        "scope": {
            "trace": str(trace),
            "outputs_dir": str(outputs_dir),
            "inputs_dir": str(inputs_dir),
            "input_cases": len(inputs),
            "output_cases": len(outputs),
            "cases_with_trace": len(per_case_events),
            "trace_event_rows": len(events),
            "observable_tool_result_consumed_events": observed_tool_events,
            "workflow_planned_fetch_invocations": sum(planned_by_tool.values()),
            "planned_fetches_without_observed_result": sum(no_observed_result_by_tool.values()),
            "planned_fetches_without_observed_result_by_tool": dict(
                sorted(no_observed_result_by_tool.items())
            ),
            "tool_call_attempts": None,
            "audit_semantics": (
                "Planned fetches mirror current source call sites; observed results are "
                "tool_result_consumed rows. A missing row means no recorded consumed result, "
                "not necessarily a network failure. Retries and hidden calls are unknown."
            ),
            "private_efficiency_score": None,
        },
        "totals": {
            "distinct_cases": len(case_ids),
            "tool_names_observed": len(tool_rows),
            "observed_evidence_refs_emitted": observed_evidence_refs,
            "observed_refs_listed_in_final_output": output_referenced,
            "observed_refs_listed_in_claims": claim_referenced,
            "output_refs_without_observed_tool_event": len(unassigned_output_refs),
            "tool_result_consumed_events_missing_tool_name": missing_tool_name_events,
            "input_cases_by_topic": dict(sorted(topic_counts.items())),
            "refund_timeline_consumed_results_by_topic": dict(
                sorted(refund_observed_by_topic.items())
            ),
        },
        "tools": sorted(tool_rows.values(), key=lambda r: r["tool_name"]),
        "cases": case_rows,
        "recommendations": recommendations,
        "warnings": warnings,
        "limitations": [
            "Trace events show recorded consumed results, not every MCP attempt.",
            (
                "Planned fetches mirror current workflow call sites; retries and server "
                "accounting are hidden."
            ),
            (
                "Evidence refs listed in output/claims do not prove the payload supported "
                "a conclusion."
            ),
            (
                "Domain-output checks are heuristics, not causal attribution or private "
                "scorer metrics."
            ),
            (
                "The private efficiency score, server budget, and prior audit history "
                "are unavailable offline."
            ),
        ],
    }


def _table(headers: list[str], rows: Iterable[list[Any]]) -> str:
    materialized = [[str(v) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in materialized:
        widths = [max(w, len(row[i]) if i < len(row) else 0) for i, w in enumerate(widths)]
    fmt = " | ".join(f"{{:<{w}}}" for w in widths)
    divider = "-+-".join("-" * w for w in widths)
    lines = [fmt.format(*headers), divider]
    lines.extend(fmt.format(*(row + [""] * (len(headers) - len(row)))) for row in materialized)
    return "\n".join(lines)


def _text_report(report: dict[str, Any], include_cases: bool) -> str:
    s = report["scope"]
    t = report["totals"]
    lines = [
        "MCP TOOL CALL AUDIT (offline)",
        (
            f"Cases: inputs={s['input_cases']}, outputs={s['output_cases']}, "
            f"traced={s['cases_with_trace']}"
        ),
        (
            f"Trace rows={s['trace_event_rows']}; observable consumed results="
            f"{s['observable_tool_result_consumed_events']}; attempts=UNKNOWN"
        ),
        (
            f"Evidence refs={t['observed_evidence_refs_emitted']}; listed in output="
            f"{t['observed_refs_listed_in_final_output']}; listed in claims="
            f"{t['observed_refs_listed_in_claims']}"
        ),
        "Listed refs do not prove semantic use. Private score is unavailable offline.",
        "",
        "BY TOOL (event = recorded tool_result_consumed row; not total MCP attempts)",
        _table(
            [
                "tool",
                "observed",
                "planned",
                "no result",
                "cases",
                "refs",
                "out refs",
                "claim refs",
                "domain signal",
                "role",
            ],
            (
                [
                    r["tool_name"],
                    r["observed_consumed_result_events"],
                    r["workflow_planned_fetch_invocations"],
                    r["planned_fetches_without_observed_result"],
                    r["cases_with_observed_result"],
                    r["evidence_refs_emitted"],
                    r["evidence_refs_listed_in_output"],
                    r["evidence_refs_listed_in_claims"],
                    f"{r['cases_with_domain_output_signal']}/{r['cases_with_observed_result']}",
                    r["role"],
                ]
                for r in report["tools"]
            ),
        ),
    ]
    if include_cases:
        lines.extend(
            [
                "",
                "BY CASE",
                _table(
                    [
                        "case",
                        "input topic",
                        "trace tool events",
                        "refs",
                        "out refs",
                        "claim refs",
                        "domain signal tools",
                        "no signal",
                    ],
                    (
                        [
                            r["case_id"],
                            r["input_topic"],
                            r["observable_consumed_result_events"],
                            r["evidence_refs_emitted"],
                            r["evidence_refs_listed_in_final_output"],
                            r["evidence_refs_listed_in_claims"],
                            ",".join(r["tools_with_domain_output_signal"]) or "-",
                            ",".join(r["tools_without_domain_output_signal"]) or "-",
                        ]
                        for r in report["cases"]
                    ),
                ),
            ]
        )
    lines.extend(["", "RECOMMENDATIONS"])
    lines.extend(
        f"[{r['priority']}] {r['finding']}\n  Action: {r['action']}\n  Evidence: {r['evidence']}"
        for r in report["recommendations"]
    )
    if report["warnings"]:
        lines.extend(["", "WARNINGS", *[f"- {x}" for x in report["warnings"]]])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace", type=Path, default=DEFAULT_TRACE, help=f"JSONL trace (default: {DEFAULT_TRACE})"
    )
    parser.add_argument(
        "--outputs",
        type=Path,
        default=DEFAULT_OUTPUTS,
        help=f"directory of output JSONs (default: {DEFAULT_OUTPUTS})",
    )
    parser.add_argument(
        "--inputs",
        type=Path,
        default=DEFAULT_INPUTS,
        help=f"directory of input JSONs (default: {DEFAULT_INPUTS})",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text", help="report format")
    parser.add_argument("--summary-only", action="store_true", help="omit the per-case table")
    args = parser.parse_args(argv)
    report = audit(args.trace, args.outputs, args.inputs)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_text_report(report, include_cases=not args.summary_only))
    return 1 if report["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
