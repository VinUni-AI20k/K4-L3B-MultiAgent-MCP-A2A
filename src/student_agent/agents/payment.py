from __future__ import annotations

from typing import Any
from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector
from .business_checks import reconcile_payments


def rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list): return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("payments", "transactions", "refunds", "timeline", "events"):
            if isinstance(value.get(key), list): return [x for x in value[key] if isinstance(x, dict)]
        return [value]
    return []


async def run(task: Task, collector: EvidenceCollector) -> Result:
    refs: list[str] = []; payments: list[dict[str, Any]] = []; refunds: list[dict[str, Any]] = []
    references: list[str] = []
    for order_id in task.entity_scope:
        for tool, target in (("get_order_payments", payments), ("get_payment_timeline", payments), ("get_refund_timeline", refunds)):
            if tool not in collector.tools: continue
            try:
                response = await collector.call(task.recipient, tool, order_id=order_id)
                ref = response["evidence_ref"]; collector.consume(task.recipient, [ref]); refs.append(ref)
                target.extend(rows(response.get("data")))
                for row in rows(response.get("data")):
                    for key in ("payment_reference", "payment_id", "transaction_id", "reference"):
                        if row.get(key): references.append(str(row[key])); break
            except Exception:
                continue
    result = reconcile_payments(payments, refunds)
    payload = {"payment_analysis": {"verdict": result["verdict"], "captured_total_brl": float(result["captured"]) if payments else None, "refunded_total_brl": float(result["refunded"]) if refunds else None, "refundable_total_brl": float(result["refundable"]) if payments else None}, "payment_references": list(dict.fromkeys(references))[:20]}
    return Result(task.case_id, task.recipient, task.message_id, payload, tuple(dict.fromkeys(refs)))
