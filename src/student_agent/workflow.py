"""L3B multi-agent workflow (rule-based, no LLM).

Luong: Coordinator -> EntityAgent -> (OrderAgent, ShipmentAgent, PaymentAgent, CustomerAgent)
       -> PolicyAgent -> VerifierAgent -> output.
Moi evidence_ref deu lay nguyen van tu MCP, khong tu sinh / sua.
Raw evidence duoc luu vao debug/raw/<case_id>.json de phan tich offline (khong nop).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

HEX32 = re.compile(r"^[0-9a-f]{32}$")
DEBUG_DIR = Path("debug") / "raw"

ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}
LATE_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}
REFUND_ISSUES = {"refund_pending", "refund_failed"}
PAYMENT_ISSUES = {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
FULL_REFUND_ISSUES = {"canceled_order_paid", "unavailable_order_paid"}

# Domain evidence dua vao output theo tung loai issue (tranh trich evidence khong lien quan).
RELEVANT_DOMAINS = {
    "late_delivery_seller": {"policy", "order", "item", "shipment", "customer"},
    "late_delivery_logistics": {"policy", "order", "item", "shipment", "customer"},
    "canceled_order_paid": {"policy", "order", "payment", "customer"},
    "unavailable_order_paid": {"policy", "order", "item", "payment", "customer"},
    "valid_split_payment": {"policy", "order", "payment", "customer"},
    "payment_mismatch": {"policy", "order", "payment", "customer"},
    "duplicate_charge": {"policy", "order", "payment", "customer"},
    "refund_pending": {"policy", "order", "payment", "refund", "customer"},
    "refund_failed": {"policy", "order", "payment", "refund", "customer"},
    "unsupported_claim": {"policy", "order", "shipment", "payment", "customer"},
    "insufficient_evidence": {"policy", "order", "customer"},
}


# ---------------------------------------------------------------- helpers
def _num(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _uniq(values: list[Any]) -> list[str]:
    seen: list[str] = []
    for v in values:
        if v is not None and str(v) not in seen:
            seen.append(str(v))
    return seen


class GatewayDown(Exception):
    """Mat ket noi MCP: huy case hien tai de CLI ket noi lai va chay lai case."""


class CaseContext:
    """Bo nho dung chung cho 1 case: cache call MCP + evidence da tieu thu."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter):
        self.case = case
        self.case_id: str = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        self.cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = {}
        self.refs_by_domain: dict[str, list[str]] = {}
        self.raw: dict[str, Any] = {}
        self.errors: list[str] = []

    async def fetch(self, actor: str, tool: str, **args: str) -> dict[str, Any] | None:
        key = (tool, tuple(sorted(args.items())))
        if key in self.cache:  # cache trong pham vi case: khong goi lap
            return self.cache[key]
        evidence: dict[str, Any] | None = None
        try:
            evidence = await self.gateway.call(tool, case_id=self.case_id, **args)
        except (RuntimeError, ValueError) as exc:  # server tu choi / sai contract -> khong retry
            self.errors.append(f"{tool}: {exc}")
        except Exception as exc:  # noqa: BLE001 - loi mang: bao CLI ket noi lai
            raise GatewayDown(f"{tool}: {exc!r}") from exc
        self.cache[key] = evidence
        self.raw[tool] = {"args": args, "evidence": evidence}
        if evidence is not None:
            ref = evidence["evidence_ref"]
            self.refs_by_domain.setdefault(evidence["domain"], [])
            if ref not in self.refs_by_domain[evidence["domain"]]:
                self.refs_by_domain[evidence["domain"]].append(ref)
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[ref],
                attributes={"domain": evidence["domain"]},
            )
        return evidence

    def assign(self, target: str, task: str) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
            decision_code=task,
        )

    def handoff(self, actor: str, target: str, code: str, refs: list[str] | None = None) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=actor,
            target=target,
            decision_code=code,
            evidence_refs=(refs or None),
        )

    def refs(self, *domains: str) -> list[str]:
        out: list[str] = []
        for d in domains:
            out.extend(self.refs_by_domain.get(d, []))
        return out


# ---------------------------------------------------------------- specialist agents
async def entity_agent(ctx: CaseContext) -> dict[str, Any]:
    """Resolve order tu claimed_order_id + candidate list; reject candidate sai dinh dang."""
    ctx.assign("entity-agent", "resolve_order")
    req = ctx.case.get("customer_request", {})
    claimed = req.get("claimed_order_id")
    candidates = [str(c) for c in ctx.case.get("candidate_order_ids", [])]
    ordered = _uniq(([claimed] if claimed else []) + candidates)
    rejected: list[str] = []
    resolved: str | None = None
    order_data: dict[str, Any] | None = None
    for cand in ordered:
        if not HEX32.match(cand):  # reject khong ton call
            rejected.append(cand)
            continue
        if resolved:
            rejected.append(cand)
            continue
        ev = await ctx.fetch("entity-agent", "get_order", order_id=cand)
        if ev and isinstance(ev.get("data"), dict) and ev["data"].get("order_id") == cand:
            resolved, order_data = cand, ev["data"]
        else:
            rejected.append(cand)
    status = "resolved" if resolved else "not_found"
    ctx.handoff("entity-agent", "coordinator", f"entity_{status}", ctx.refs("order"))
    return {
        "status": status,
        "order_id": resolved,
        "order": order_data or {},
        "rejected": rejected,
        "confidence": 0.95 if resolved else 0.2,
    }


async def customer_agent(ctx: CaseContext, order_id: str) -> dict[str, Any]:
    ctx.assign("customer-agent", "customer_history")
    uid = ctx.case.get("customer_unique_id_hint")
    rows: list[dict[str, Any]] = []
    if uid:
        ev = await ctx.fetch("customer-agent", "get_customer_history", customer_unique_id=uid)
        if ev and isinstance(ev.get("data"), dict):
            uid = ev["data"].get("customer_unique_id", uid)
            rows = [r for r in ev["data"].get("orders", []) if isinstance(r, dict)]
    ctx.handoff("customer-agent", "coordinator", "customer_context_ready", ctx.refs("customer"))
    return {
        "customer_unique_id": uid,
        "rows": rows,
        "related_order_ids": _uniq([r.get("order_id") for r in rows]) or [order_id],
    }


async def order_agent(ctx: CaseContext, order_id: str) -> dict[str, Any]:
    ctx.assign("order-agent", "order_items")
    ev = await ctx.fetch("order-agent", "get_order_items", order_id=order_id)
    items = [r for r in (ev or {}).get("data") or [] if isinstance(r, dict)]
    ctx.handoff("order-agent", "coordinator", "items_ready", ctx.refs("item"))
    return {
        "items": items,
        "item_ids": _uniq([r.get("order_item_id") for r in items]),
        "seller_ids": _uniq([r.get("seller_id") for r in items]),
    }


KEY_DATES = (
    "order_purchase_timestamp",
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
)


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(k) for k in (*KEY_DATES, "order_status"))


def conflict_resolver(
    ctx: CaseContext, order: dict[str, Any], cust: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Cung 1 order_id co 2 ban ghi (get_order vs customer history).

    Ban ghi kich ban cua case = ban ghi trong history KHAC voi get_order.
    Moi event duoc gan ve ban ghi co moc thoi gian gan nhat; event cua ban ghi con lai la nhieu.
    """
    ctx.assign("conflict-resolver", "select_case_timeline")
    oid = order.get("order_id")
    rows = [r for r in cust["rows"] if r.get("order_id") == oid] or [order]
    others = [r for r in rows if _row_key(r) != _row_key(order)]
    scenario = others[0] if others else rows[-1]
    ambiguous = not others

    def owns(ts: Any) -> bool:
        t = _dt(ts)
        if t is None:
            return True

        def dist(r: dict[str, Any]) -> float:
            ds = [abs((t - d).total_seconds()) for k in KEY_DATES if (d := _dt(r.get(k)))]
            return min(ds) if ds else float("inf")

        best = min(rows, key=lambda r: (dist(r), 0 if _row_key(r) == _row_key(scenario) else 1))
        return _row_key(best) == _row_key(scenario)

    start = _dt(scenario.get("order_purchase_timestamp"))
    item = None
    if items and start:
        item = min(
            items,
            key=lambda i: abs(
                ((_dt(i.get("shipping_limit_date")) or start) - start).total_seconds()
            ),
        )
    conflicts: list[dict[str, Any]] = []
    if others:
        for field in (*KEY_DATES, "order_status"):
            if str(order.get(field)) != str(scenario.get(field)):
                conflicts.append(
                    {
                        "field": field,
                        "sources": ["get_order", "get_customer_history"],
                        "selected_source": "get_customer_history",
                        "resolution_code": "case_timeline_row_selected",
                    }
                )
    ctx.handoff(
        "conflict-resolver",
        "coordinator",
        "timeline_ambiguous" if ambiguous else "timeline_selected",
        ctx.refs("order", "customer"),
    )
    return {
        "row": scenario,
        "owns": owns,
        "item": item or (items[0] if items else {}),
        "ambiguous": ambiguous,
        "conflicts": conflicts[:5],
    }


async def shipment_agent(ctx: CaseContext, order_id: str, scen: dict[str, Any]) -> dict[str, Any]:
    ctx.assign("shipment-agent", "shipment_timeline")
    ev = await ctx.fetch("shipment-agent", "get_shipment_summary", order_id=order_id)
    data = (ev or {}).get("data") or {}
    events = [
        e
        for e in data.get("events", [])
        if isinstance(e, dict)
        and str(e.get("status", "confirmed")) == "confirmed"
        and scen["owns"](e.get("event_at"))
    ]
    late = [e for e in events if "late" in str(e.get("event_type", ""))]
    row, item = scen["row"], scen["item"]
    status = str(row.get("order_status"))
    carrier = _dt(row.get("order_delivered_carrier_date"))
    delivered = _dt(row.get("order_delivered_customer_date"))
    estimated = _dt(row.get("order_estimated_delivery_date"))
    limit_at = _dt(item.get("shipping_limit_date"))
    seller_late = bool(carrier and limit_at and carrier > limit_at)

    if late:
        verdict = "seller_delay" if str(late[0].get("actor")) == "seller" else "logistics_delay"
    elif status in {"canceled", "unavailable"}:
        verdict = "insufficient_evidence"
    elif any("lost" in str(e.get("event_type")) for e in events):
        verdict = "lost"
    elif any("return" in str(e.get("event_type")) for e in events):
        verdict = "returned"
    elif delivered and estimated:
        if delivered > estimated:
            verdict = "seller_delay" if seller_late else "logistics_delay"
        else:
            verdict = "on_time"
    else:
        verdict = "insufficient_evidence"
    late_sellers = [str(item.get("seller_id"))] if verdict == "seller_delay" and item else []
    ctx.handoff("shipment-agent", "coordinator", f"shipment_{verdict}", ctx.refs("shipment"))
    return {
        "verdict": verdict,
        "late_sellers": late_sellers,
        "timeline_complete": bool(carrier and delivered and estimated),
        "status": status,
    }


async def payment_agent(
    ctx: CaseContext, order_id: str, scen: dict[str, Any], want_refund: bool
) -> dict[str, Any]:
    ctx.assign("payment-agent", "payment_refund_timeline")
    ev = await ctx.fetch("payment-agent", "get_payment_timeline", order_id=order_id)
    data = (ev or {}).get("data") or {}
    events = [
        e for e in data.get("events", []) if isinstance(e, dict) and scen["owns"](e.get("event_at"))
    ]
    refund_events: list[dict[str, Any]] = []
    if want_refund or any("refund" in str(e.get("event_type", "")) for e in events):
        rev = await ctx.fetch("payment-agent", "get_refund_timeline", order_id=order_id)
        rdata = (rev or {}).get("data") or {}
        raw = rdata.get("events", []) if isinstance(rdata, dict) else rdata
        refund_events = [e for e in raw or [] if isinstance(e, dict)]

    captures = [
        _num(e.get("amount_brl")) or 0.0
        for e in events
        if e.get("event_type") == "captured" and e.get("status") == "confirmed"
    ]
    item = scen["item"]
    order_total = (_num(item.get("price")) or 0) + (_num(item.get("freight_value")) or 0)
    split = duplicate = False
    for amount, n in Counter(captures).items():
        if n >= 2:
            if abs(2 * amount - order_total) < 0.01:
                split = True
            else:
                duplicate = True
    if scen["ambiguous"] and split:
        captures = [a for a in captures if Counter(captures)[a] >= 2]
    refund_status = {str(e.get("status")) for e in refund_events}
    refunded = round(
        sum(
            _num(e.get("amount_brl")) or 0
            for e in refund_events
            if str(e.get("status")) in {"succeeded", "completed", "confirmed", "refunded"}
        ),
        2,
    )
    ctx.handoff("payment-agent", "coordinator", "payment_ready", ctx.refs("payment", "refund"))
    return {
        "captured_total": round(sum(captures), 2) if captures else None,
        "refunded_total": refunded,
        "refund_failed": "failed" in refund_status,
        "refund_pending": "pending" in refund_status,
        "mismatch": any(e.get("event_type") == "reconciliation_mismatch" for e in events),
        "split": split,
        "duplicate": duplicate,
    }


# ---------------------------------------------------------------- policy + verifier
def classify(ship: dict[str, Any], pay: dict[str, Any]) -> str:
    """Xac dinh issue CHI tu evidence (khong tin noi dung khieu nai)."""
    if ship["status"] == "canceled":
        return "canceled_order_paid"
    if ship["status"] == "unavailable":
        return "unavailable_order_paid"
    if ship["verdict"] == "seller_delay":
        return "late_delivery_seller"
    if ship["verdict"] == "logistics_delay":
        return "late_delivery_logistics"
    if pay["refund_failed"]:
        return "refund_failed"
    if pay["refund_pending"]:
        return "refund_pending"
    if pay["mismatch"]:
        return "payment_mismatch"
    if pay["duplicate"]:
        return "duplicate_charge"
    if pay["split"]:
        return "valid_split_payment"
    return "unsupported_claim"


def payment_verdict_for(issue: str, pay: dict[str, Any]) -> str:
    mapping = {
        "duplicate_charge": "duplicate_capture",
        "payment_mismatch": "capture_mismatch",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
        "valid_split_payment": "reconciled",
    }
    if issue in mapping:
        return mapping[issue]
    if pay["captured_total"] is None:
        return "insufficient_evidence"
    if pay["refunded_total"]:
        return "refunded"
    return "reconciled"


def verify(output: dict[str, Any]) -> list[str]:
    """Kiem tra nhat quan truoc khi finalize. Tra ve danh sach loi (rong = pass)."""
    problems: list[str] = []
    a = output["assessment"]
    fin = output["financial_resolution"]
    total = round(sum(line["amount_brl"] for line in fin["refund_lines"]), 2)
    if abs(total - fin["recommended_refund_brl"]) > 0.01:
        problems.append("refund_lines_sum_mismatch")
    if a["case_status"] == "no_action" and fin["recommended_refund_brl"] > 0:
        problems.append("no_action_with_refund")
    if a["case_status"] == "action_required" and not output["resolution_actions"]:
        problems.append("action_required_without_action")
    parties = output["root_cause_analysis"]["responsible_parties"]
    types = {p["party_type"] for p in parties}
    if a["primary_issue"] == "late_delivery_logistics" and "seller" in types:
        problems.append("logistics_issue_blames_seller")
    sellers = set(output["affected_entities"]["seller_ids"])
    for p in parties:
        if p["party_type"] == "seller" and p["party_id"] not in sellers:
            problems.append("responsible_seller_not_in_entities")
    if not output["evidence_refs"]:
        problems.append("no_evidence")
    return problems


# ---------------------------------------------------------------- coordinator
async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    ctx = CaseContext(case, gateway, trace)
    try:
        output = await _solve(ctx)
    except GatewayDown:
        raise
    except Exception as exc:  # noqa: BLE001 - 1 case loi khong duoc lam hong ca batch
        ctx.errors.append(f"solver: {exc!r}")
        output = _fallback(ctx)
    _dump_debug(ctx, output)
    return output


async def _solve(ctx: CaseContext) -> dict[str, Any]:
    case = ctx.case
    req = case.get("customer_request", {})
    claims = [c for c in req.get("claims", []) if isinstance(c, dict)]
    claimed = next((c.get("topic") for c in claims if c.get("topic") in ISSUES), None)

    # Policy (can cho moi case)
    ctx.assign("policy-agent", "load_policy")
    pol_ev = await ctx.fetch(
        "policy-agent", "get_policy", policy_version=str(case.get("policy_version", ""))
    )
    rules = ((pol_ev or {}).get("data") or {}).get("rules", {})

    entity = await entity_agent(ctx)
    if not entity["order_id"]:
        return _fallback(ctx, entity=entity)
    oid = entity["order_id"]

    cust = await customer_agent(ctx, oid)
    items = await order_agent(ctx, oid)
    scen = conflict_resolver(ctx, entity["order"], cust, items["items"])
    ship = await shipment_agent(ctx, oid, scen)
    pay = await payment_agent(ctx, oid, scen, want_refund=claimed in REFUND_ISSUES)

    issue = classify(ship, pay)
    confidence = 0.95 if issue == claimed else 0.6
    if scen["ambiguous"]:
        confidence = min(confidence, 0.85)
    rule = rules.get(issue) or {}
    refund = _num(rule.get("refund_brl")) or 0.0
    status = rule.get("case_status") or ("no_action" if refund == 0 else "action_required")
    action = rule.get("recommended_action")
    parties = [
        {"party_type": p.get("party_type", "unknown"), "party_id": p.get("party_id")}
        for p in rule.get("responsible_parties", [])
        if isinstance(p, dict)
    ][:5] or [{"party_type": "unknown", "party_id": None}]
    seller_id = scen["item"].get("seller_id")
    parties = [
        {"party_type": "seller", "party_id": seller_id}
        if p["party_type"] == "seller" and seller_id
        else p
        for p in parties
    ]
    if not rule:
        confidence = min(confidence, 0.4)
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue,
        evidence_refs=ctx.refs("policy") or None,
        attributes={"case_status": status, "refund_brl": refund},
    )
    ctx.handoff("policy-agent", "verifier-agent", "policy_decided", ctx.refs("policy"))

    domains = RELEVANT_DOMAINS.get(issue, {"policy", "order"})
    evidence_refs = _uniq(ctx.refs(*sorted(domains)))[:30]
    conflicts = scen["conflicts"]

    claim_assessments = []
    for c in claims[:5]:
        topic = c.get("topic")
        if topic == "requested_full_refund":
            verdict = (
                "supported"
                if issue in FULL_REFUND_ISSUES and refund > 0
                else "partially_supported"
                if refund > 0
                else "unsupported"
            )
        else:
            verdict = "supported" if topic == issue else "unsupported"
        claim_assessments.append(
            {
                "claim_id": str(c.get("claim_id", "claim"))[:64],
                "verdict": verdict,
                "confidence": round(confidence, 2),
                "evidence_refs": evidence_refs[:30],
            }
        )

    late_sellers = ship["late_sellers"] if issue == "late_delivery_seller" else []
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": status,
            "confidence": round(confidence, 2),
        },
        "affected_entities": {
            "order_ids": [oid],
            "item_ids": items["item_ids"][:20],
            "seller_ids": items["seller_ids"][:20],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": [oid],
            "rejected_candidates": entity["rejected"][:20],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": cust["customer_unique_id"],
            "related_order_ids": cust["related_order_ids"][:20],
        },
        "shipment_analysis": {
            "verdict": ship["verdict"],
            "late_seller_ids": _uniq(late_sellers)[:20],
            "timeline_complete": ship["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment_verdict_for(issue, pay),
            "captured_total_brl": pay["captured_total"],
            "refunded_total_brl": pay["refunded_total"],
            "refundable_total_brl": refund,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": (
                [{"reason_code": action or issue, "amount_brl": refund, "entity_id": oid}]
                if refund > 0
                else []
            ),
        },
        "resolution_actions": [action] if action else [],
    }

    problems = verify(output)
    if problems:
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.5)
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="passed" if not problems else "passed_with_warnings",
        evidence_refs=evidence_refs[:20] or None,
        attributes={"problems": ",".join(problems)[:200] or None},
    )
    ctx.handoff("verifier-agent", "coordinator", "verified")
    return output


def _fallback(ctx: CaseContext, entity: dict[str, Any] | None = None) -> dict[str, Any]:
    """Output an toan khi thieu evidence: khong bia du lieu, khong tu tao ref."""
    refs = _uniq(ctx.refs(*ctx.refs_by_domain.keys()))[:30]
    candidates = [str(c) for c in ctx.case.get("candidate_order_ids", [])]
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="fallback_insufficient_evidence",
        evidence_refs=refs[:20] or None,
    )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": (entity or {}).get("status", "not_found"),
            "resolved_order_ids": [],
            "rejected_candidates": ((entity or {}).get("rejected") or candidates)[:20],
            "confidence": 0.2,
        },
        "customer_context": {
            "customer_unique_id": ctx.case.get("customer_unique_id_hint"),
            "related_order_ids": [],
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def _dump_debug(ctx: CaseContext, output: dict[str, Any]) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{ctx.case_id}.json").write_text(
            json.dumps(
                {"case": ctx.case, "calls": ctx.raw, "errors": ctx.errors, "output": output},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass