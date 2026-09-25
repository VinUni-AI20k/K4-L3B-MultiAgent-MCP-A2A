from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .model_agent import ModelSettings, ModelVerifier
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case

REQUIRED_TOOLS = {
    "get_order",
    "get_order_items",
    "get_customer_history",
    "get_product_context",
    "get_order_payments",
    "get_payment_timeline",
    "get_refund_timeline",
    "get_shipment_summary",
    "get_sellers",
    "get_policy",
}


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _stage_progress(staged: Path, case_set: CaseSet, contracts: Contracts) -> int:
    """Keep only a contiguous prefix of finalized cases after an interrupted run."""
    output_root = staged / "outputs"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path = staged / "traces" / "trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    lines = trace_path.read_text(encoding="utf-8").splitlines() if trace_path.exists() else []
    events = [json.loads(line) for line in lines if line.strip()]
    finalized = {
        event["case_id"] for event in events if event.get("event_type") == "case_finalized"
    }
    count = 0
    for case_id in case_set.case_ids:
        path = output_root / f"{case_id}.json"
        if not path.exists() or case_id not in finalized:
            break
        output = json.loads(path.read_text(encoding="utf-8"))
        contracts.validate_output(output, f"staged/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"staged/{case_id}.json has wrong case ID")
        count += 1
    completed = set(case_set.case_ids[:count])
    for path in output_root.glob("*.json"):
        if path.stem not in completed:
            path.unlink()
    kept = [event for event in events if event.get("case_id") in completed]
    trace_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in kept
        ),
        encoding="utf-8",
    )
    return count


def _publish(root: Path, staged: Path) -> None:
    """Replace a completed run while preserving the previous local artifacts."""
    outputs = root / "outputs"
    trace = root / "traces" / "trace.jsonl"
    backup = root / ".run-backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=True)
    moved_outputs = False
    try:
        if outputs.exists():
            outputs.rename(backup / "outputs")
            moved_outputs = True
        (staged / "outputs").rename(outputs)
        trace.parent.mkdir(parents=True, exist_ok=True)
        if trace.exists():
            trace.rename(backup / "trace.jsonl")
        (staged / "traces" / "trace.jsonl").rename(trace)
    except OSError:
        if not outputs.exists() and moved_outputs:
            (backup / "outputs").rename(outputs)
        if not trace.exists() and (backup / "trace.jsonl").exists():
            (backup / "trace.jsonl").rename(trace)
        raise
    if not any(backup.iterdir()):
        backup.rmdir()
    (staged / "traces").rmdir()
    staged.rmdir()


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    model = ModelVerifier(ModelSettings.load())
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    staged = root / ".day09-work"
    completed = _stage_progress(staged, case_set, contracts)
    print(f"Resuming at {completed}/{len(case_set.case_ids)} completed cases", flush=True)
    output_root = staged / "outputs"
    trace = TraceWriter(staged / "traces" / "trace.jsonl", contracts)
    if completed < len(case_set.case_ids):
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            tools = set(await gateway.list_tools())
            missing = REQUIRED_TOOLS - tools
            if missing:
                raise RuntimeError(f"MCP Gateway is missing tools: {sorted(missing)}")
            for index in range(completed, len(case_set.case_ids)):
                case_id = case_set.case_ids[index]
                case = case_set.cases[case_id]
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                output = await solve_case(case, gateway, trace, model, tools)
                contracts.validate_output(output, f"outputs/{case_id}.json")
                if output.get("case_id") != case_id:
                    raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                if output["entity_resolution"]["status"] != "resolved":
                    raise RuntimeError(f"{case_id}: entity unresolved; preserving staged progress")
                target = output_root / f"{case_id}.json"
                temporary = target.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                temporary.replace(target)
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                if (index + 1) % 10 == 0 or index + 1 == len(case_set.case_ids):
                    print(f"Completed {index + 1}/{len(case_set.case_ids)} cases", flush=True)
    validate_artifacts(staged, case_set, contracts)
    _publish(root, staged)
    print("Completed and validated 100 cases; outputs and trace published locally")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run or resume the workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
