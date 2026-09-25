"""Recompute selected cases and keep one complete trace segment per case."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from student_agent.cases import load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


def complete_segment(lines: list[str], case_id: str) -> list[str]:
    current: list[str] = []
    completed: list[str] = []
    for line in lines:
        event = json.loads(line)
        if event["case_id"] != case_id:
            continue
        if event["event_type"] == "case_received":
            current = []
        current.append(line)
        if event["event_type"] == "case_finalized":
            completed = current
    if not completed:
        raise ValueError(f"{case_id} has no complete trace segment")
    return completed


async def repair(root: Path, case_ids: list[str]) -> None:
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    settings = Settings.load(root)
    for case_id in case_ids:
        if case_id not in case_set.cases:
            raise ValueError(f"unknown case: {case_id}")

    replay_dir = root / "traces" / "replays"
    replay_dir.mkdir(parents=True, exist_ok=True)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for case_id in case_ids:
            replay_path = replay_dir / f"{case_id}.jsonl"
            replay_path.unlink(missing_ok=True)
            trace = TraceWriter(replay_path, contracts)
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case_set.cases[case_id], gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output["case_id"] != case_id:
                raise ValueError(f"solver returned wrong case_id for {case_id}")
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            target = root / "outputs" / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            print(f"repaired {case_id}: {output['assessment']['primary_issue']}", flush=True)

    original = (root / "traces" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    merged: list[str] = []
    for case_id in case_set.case_ids:
        replay_path = replay_dir / f"{case_id}.jsonl"
        lines = (
            replay_path.read_text(encoding="utf-8").splitlines()
            if replay_path.exists()
            else original
        )
        merged.extend(complete_segment(lines, case_id))
    target = root / "traces" / "trace.jsonl"
    temporary = target.with_suffix(".jsonl.tmp")
    temporary.write_text("\n".join(merged) + "\n", encoding="utf-8")
    temporary.replace(target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_ids", nargs="+", help="case IDs to recompute")
    args = parser.parse_args()
    asyncio.run(repair(Path.cwd(), args.case_ids))


if __name__ == "__main__":
    main()
