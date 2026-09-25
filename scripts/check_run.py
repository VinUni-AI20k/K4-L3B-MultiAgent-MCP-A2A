"""Kiem tra lan chay gan nhat (KHONG goi MCP). Chay: python scripts/check_run.py"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path.cwd()
raw_dir = ROOT / "debug" / "raw"
trace_refs: dict[str, set[str]] = defaultdict(set)
for line in (ROOT / "traces" / "trace.jsonl").read_text(encoding="utf-8").splitlines():
    if line.strip():
        ev = json.loads(line)
        trace_refs[ev["case_id"]].update(ev.get("evidence_refs", []))

ref_owner: dict[str, str] = {}
problems: list[str] = []
issues: Counter[str] = Counter()
for out_path in sorted((ROOT / "outputs").glob("*.json")):
    case_id = out_path.stem
    out = json.loads(out_path.read_text(encoding="utf-8"))
    issues[out["assessment"]["primary_issue"]] += 1
    raw_path = raw_dir / f"{case_id}.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {}
    errs = raw.get("errors", [])
    failed = [t for t, v in raw.get("calls", {}).items() if v.get("evidence") is None]
    topic = raw.get("case", {}).get("customer_request", {}).get("claims", [{}])[0].get("topic")
    if errs or failed:
        problems.append(f"{case_id} [{topic}] tool loi: {failed} | {errs[:2]}")
    if out["assessment"]["primary_issue"] != topic:
        problems.append(f"{case_id} issue={out['assessment']['primary_issue']} != claim={topic}")
    missing = [r for r in out["evidence_refs"] if r not in trace_refs[case_id]]
    if missing:
        problems.append(f"{case_id} ref trong output khong co trong trace: {len(missing)}")
    for r in out["evidence_refs"]:
        if r in ref_owner and ref_owner[r] != case_id:
            problems.append(f"{case_id} ref trung voi case {ref_owner[r]}")
        ref_owner[r] = case_id

print("Phan bo primary_issue:", dict(issues))
print(f"So van de: {len(problems)}")
for p in problems:
    print(" -", p)
