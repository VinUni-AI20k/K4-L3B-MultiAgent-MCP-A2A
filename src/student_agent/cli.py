from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


def _error_message(error: Exception) -> str:
    """Expose MCP failures nested by the transport's async task group."""
    if isinstance(error, ExceptionGroup):
        return "; ".join(dict.fromkeys(_error_message(item) for item in error.exceptions))
    return str(error)


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    await _ensure_run(settings, case_set.version)
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    # Stage the complete run; MCP failures must leave existing artifacts intact.
    with tempfile.TemporaryDirectory(prefix="day09-run-", dir=root) as staged:
        await _generate_run(root, Path(staged), settings, case_set, contracts)
        staged_root = Path(staged)
        for case_id in case_set.case_ids:
            (staged_root / "outputs" / f"{case_id}.json").replace(output_root / f"{case_id}.json")
        (staged_root / "traces" / "trace.jsonl").replace(trace_path)


async def _ensure_run(settings: Settings, case_set_version: str) -> None:
    """The Gateway requires an active team run, even when tools/list succeeds."""
    async with httpx2.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{settings.competition_api_url}/api/v2/runs",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
            json={"variant_id": "l3b"},
        )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Cannot initialize L3B run: HTTP {response.status_code}")
    run = response.json()
    if run.get("case_set_version") != case_set_version:
        raise ValueError("Local case-set differs from active run; download the current L3B inputs")
    run_endpoint = run.get("mcp_endpoint", settings.mcp_endpoint).rstrip("/")
    if run_endpoint != settings.mcp_endpoint.rstrip("/"):
        raise ValueError("MCP_ENDPOINT differs from the endpoint returned by the active run")
    print(f"Active L3B run: {case_set_version}; expires {run.get('expires_at', 'unknown')}")


async def _generate_run(root, staged_root, settings, case_set, contracts, completed=()) -> None:
    output_root = staged_root / "outputs"
    output_root.mkdir(exist_ok=True)
    trace = TraceWriter(staged_root / "traces" / "trace.jsonl", contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")

        async def solve_one(case_id):
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            print(f"OK: {case_id}; {len(output['evidence_refs'])} evidence refs", flush=True)

        semaphore = asyncio.Semaphore(4)

        async def bounded(case_id):
            async with semaphore:
                await solve_one(case_id)

        async with asyncio.TaskGroup() as tasks:
            for case_id in case_set.case_ids:
                if case_id not in completed:
                    tasks.create_task(bounded(case_id))
    validate_artifacts(staged_root, case_set, contracts)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
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
    except (OSError, RuntimeError, ValueError, ExceptionGroup) as exc:
        print(f"ERROR: {_error_message(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
