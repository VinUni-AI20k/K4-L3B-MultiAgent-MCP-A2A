from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


def _error_detail(error: Exception) -> str:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.discover_tools():
            print(json.dumps(tool, ensure_ascii=False, indent=2))


async def _run(
    root: Path,
    *,
    resume: bool = False,
    limit: int | None = None,
    progress: bool = False,
    workers: int = 1,
) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    if limit is not None and not 1 <= limit <= len(case_set.case_ids):
        raise ValueError(f"--limit must be between 1 and {len(case_set.case_ids)}")
    if not 1 <= workers <= 16:
        raise ValueError("--workers must be between 1 and 16")
    selected_case_ids = case_set.case_ids[:limit] if limit is not None else case_set.case_ids
    contracts = Contracts(root / "contracts" / "schemas")
    artifact_root = root if limit is None else root / "mock-runs" / f"first-{limit}"
    output_root = artifact_root / "outputs"
    trace_path = artifact_root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    finalized_cases: set[str] = set()
    if resume:
        if not trace_path.exists() and any(output_root.glob("*.json")):
            raise ValueError("cannot resume outputs without traces/trace.jsonl")
        if trace_path.exists():
            for number, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"traces/trace.jsonl:{number}: invalid JSON") from exc
                contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
                if event["event_type"] == "case_finalized":
                    finalized_cases.add(event["case_id"])
    else:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)
    pending: list[str] = []
    completed = 0
    for case_id in selected_case_ids:
        target = output_root / f"{case_id}.json"
        if resume and case_id in finalized_cases and target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            contracts.validate_output(existing, f"outputs/{case_id}.json")
            if existing.get("case_id") != case_id:
                raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
            completed += 1
            if progress:
                print(f"[{completed}/{len(selected_case_ids)}] {case_id}: skipped", flush=True)
        else:
            pending.append(case_id)

    if pending:
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            discovered_tools = await gateway.list_tools()
            if not discovered_tools:
                raise RuntimeError("MCP Gateway returned no tools")
            semaphore = asyncio.Semaphore(workers)

            async def run_one(case_id: str) -> None:
                nonlocal completed
                async with semaphore:
                    if progress:
                        print(
                            f"[{completed}/{len(selected_case_ids)}] {case_id}: running",
                            flush=True,
                        )
                    target = output_root / f"{case_id}.json"
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case_set.cases[case_id], gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    completed += 1
                    if progress:
                        print(f"[{completed}/{len(selected_case_ids)}] {case_id}: done", flush=True)

            async with asyncio.TaskGroup() as group:
                for case_id in pending:
                    group.create_task(run_one(case_id))

    _compact_trace(trace_path, set(selected_case_ids))


def _compact_trace(path: Path, case_ids: set[str]) -> None:
    """Keep the last complete lifecycle for each case after any resumed run."""
    lines = path.read_text(encoding="utf-8").splitlines()
    active: dict[str, list[int]] = {}
    complete: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        event = json.loads(line)
        case_id = event.get("case_id")
        if case_id not in case_ids:
            continue
        if event.get("event_type") == "case_received":
            active[case_id] = []
        if case_id in active:
            active[case_id].append(index)
            if event.get("event_type") == "case_finalized":
                complete[case_id] = active.pop(case_id)
    missing = case_ids - complete.keys()
    if missing:
        raise ValueError(f"trace is missing complete case lifecycles: {sorted(missing)}")
    keep = {index for indices in complete.values() for index in indices}
    if len(keep) != len(lines):
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text(
            "\n".join(line for index, line in enumerate(lines) if index in keep) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    sample = commands.add_parser("validate-sample", help="validate an isolated first-N run")
    sample.add_argument("--limit", type=int, required=True, metavar="N")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--resume", action="store_true", help="keep completed cases and continue")
    run.add_argument("--limit", type=int, metavar="N", help="run the first N cases in mock-runs/")
    run.add_argument("--progress", action="store_true", help="print progress for each case")
    run.add_argument("--workers", type=int, default=1, metavar="N", help="parallel cases (1-16)")
    build = commands.add_parser("build", help="run all cases, validate, and package the submission")
    build.add_argument("--resume", action="store_true", help="keep completed cases and continue")
    build.add_argument("--progress", action="store_true", help="print progress for each case")
    build.add_argument("--workers", type=int, default=1, metavar="N", help="parallel cases (1-16)")
    build.add_argument("--output", default="dist/submission.zip")
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
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "validate-sample":
            case_set = load_case_set(root)
            if not 1 <= args.limit <= len(case_set.case_ids):
                raise ValueError(f"--limit must be between 1 and {len(case_set.case_ids)}")
            sample_set = CaseSet(
                case_set.version,
                case_set.variant_id,
                case_set.case_ids[: args.limit],
                {case_id: case_set.cases[case_id] for case_id in case_set.case_ids[: args.limit]},
            )
            contracts = Contracts(root / "contracts" / "schemas")
            sample_root = root / "mock-runs" / f"first-{args.limit}"
            _, trace = validate_artifacts(sample_root, sample_set, contracts)
            print(f"OK: {args.limit} sample outputs / {len(trace)} trace events")
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(
                _run(
                    root,
                    resume=args.resume,
                    limit=args.limit,
                    progress=args.progress,
                    workers=args.workers,
                )
            )
        elif args.command == "build":
            asyncio.run(
                _run(root, resume=args.resume, progress=args.progress, workers=args.workers)
            )
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError, ExceptionGroup) as exc:
        print(f"ERROR: {_error_detail(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
