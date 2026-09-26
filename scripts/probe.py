"""Khao sat du lieu: (1) thong ke 100 input (khong goi MCP), (2) goi thu 10 tool cho 1 case.

Chay:  python scripts/probe.py L3B_CASE_001
Ket qua luu vao debug/ (da chan boi .gitignore, KHONG commit, KHONG dua vao ZIP).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from student_agent.cases import load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

ROOT = Path.cwd()
DEBUG = ROOT / "debug"
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def survey(cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    keys: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    n_claims: Counter[int] = Counter()
    n_candidates: Counter[int] = Counter()
    claimed_missing = claimed_not_hex = claimed_not_in_candidates = 0
    candidate_formats: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    policies: Counter[str] = Counter()
    messages: Counter[str] = Counter()
    for case in cases.values():
        keys.update(case.keys())
        req = case.get("customer_request", {})
        claims = req.get("claims", [])
        n_claims[len(claims)] += 1
        topics.update(c.get("topic", "?") for c in claims)
        claimed = req.get("claimed_order_id")
        cands = case.get("candidate_order_ids", [])
        n_candidates[len(cands)] += 1
        for cand in cands:
            candidate_formats["hex32" if HEX32.match(str(cand)) else "other"] += 1
        if not claimed:
            claimed_missing += 1
        else:
            if not HEX32.match(claimed):
                claimed_not_hex += 1
            if claimed not in cands:
                claimed_not_in_candidates += 1
        scopes[json.dumps(case.get("investigation_scope"), sort_keys=True)] += 1
        policies[str(case.get("policy_version"))] += 1
        messages[req.get("message", "")] += 1
    return {
        "top_level_keys": dict(keys),
        "claim_topics": dict(topics),
        "claims_per_case": dict(n_claims),
        "candidates_per_case": dict(n_candidates),
        "candidate_formats": dict(candidate_formats),
        "claimed_order_id_missing": claimed_missing,
        "claimed_order_id_not_hex32": claimed_not_hex,
        "claimed_not_in_candidates": claimed_not_in_candidates,
        "investigation_scopes": dict(scopes),
        "policy_versions": dict(policies),
        "messages": dict(messages),
    }


def find_values(obj: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            found.extend(find_values(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(find_values(v, key))
    return found


async def probe(case: dict[str, Any]) -> dict[str, Any]:
    settings = Settings.load(ROOT)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    case_id = case["case_id"]
    req = case.get("customer_request", {})
    order_id = req.get("claimed_order_id") or next(
        (c for c in case.get("candidate_order_ids", []) if HEX32.match(str(c))), None
    )
    results: dict[str, Any] = {"case_id": case_id, "order_id_used": order_id, "calls": {}}

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:

        async def call(tool: str, **args: str) -> Any:
            try:
                ev = await gw.call(tool, case_id=case_id, **args)
                results["calls"][tool] = {"args": args, "response": ev}
                print(f"  OK   {tool:24s} domain={ev.get('domain')} warnings={ev.get('warnings')}")
                return ev
            except Exception as exc:  # noqa: BLE001 - probe only
                results["calls"][tool] = {"args": args, "error": str(exc)}
                print(f"  FAIL {tool:24s} {exc}")
                return None

        await call("get_policy", policy_version=str(case.get("policy_version")))
        if order_id:
            order = await call("get_order", order_id=order_id)
            for tool in (
                "get_order_items",
                "get_order_payments",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_sellers",
                "get_shipment_summary",
                "get_product_context",
            ):
                await call(tool, order_id=order_id)
            uids = find_values(order, "customer_unique_id") if order else []
            uid = str(uids[0]) if uids else case.get("customer_unique_id_hint")
            results["customer_unique_id_used"] = uid
            if uid:
                await call("get_customer_history", customer_unique_id=uid)
    return results


def main() -> None:
    case_id = sys.argv[1] if len(sys.argv) > 1 else "L3B_CASE_001"
    DEBUG.mkdir(exist_ok=True)
    case_set = load_case_set(ROOT)

    stats = survey(case_set.cases)
    (DEBUG / "input_survey.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Survey 100 inputs -> {DEBUG / 'input_survey.json'}")

    print(f"Probe MCP cho {case_id}:")
    result = asyncio.run(probe(case_set.cases[case_id]))
    target = DEBUG / f"probe_{case_id}.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved -> {target}")


if __name__ == "__main__":
    main()
