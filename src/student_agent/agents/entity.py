from __future__ import annotations

from typing import Any

from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector


def ids(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
        raise ValueError("Unsupported identifier shape; adapt against the official input")
    return list(dict.fromkeys(values))


async def resolve(task: Task, collector: EvidenceCollector) -> Result:
    """Explicit adapter: flat order_id/order_ids, candidate_order_ids and customer_unique_id.

    Unknown evidence shapes fail closed; do not infer IDs from arbitrary nested values.
    """
    case = task.payload
    exact = ids(case.get("order_ids")) + ids(case.get("order_id"))
    exact = list(dict.fromkeys(exact))
    candidates = ids(case.get("candidate_order_ids"))
    customer = case.get("customer_unique_id")
    if customer is not None and (not isinstance(customer, str) or not customer):
        raise ValueError("Invalid customer_unique_id")
    refs, related, rejected, confirmed = [], [], [], []
    if customer:
        response = await collector.call(
            "entity-agent", "get_customer_history", customer_unique_id=customer
        )
        data = response["data"]
        if not isinstance(data, dict) or data.get("customer_unique_id") != customer:
            raise ValueError("Unsupported or mismatched customer evidence")
        related = ids(data.get("related_order_ids"))
        refs.append(response["evidence_ref"])
        collector.consume("entity-agent", [response["evidence_ref"]])
    checked = list(dict.fromkeys(exact + candidates))
    for order in checked:
        response = await collector.call("entity-agent", "get_order", order_id=order)
        data = response["data"]
        if not isinstance(data, dict) or data.get("order_id") != order:
            raise ValueError("Unsupported or mismatched order evidence")
        refs.append(response["evidence_ref"])
        collector.consume("entity-agent", [response["evidence_ref"]])
        if customer and data.get("customer_unique_id") != customer:
            # Missing identifiers are not proof of a mismatch.
            if data.get("customer_unique_id") is None:
                raise ValueError("Order evidence cannot establish customer ownership")
            rejected.append(order)
        else:
            confirmed.append(order)
    if exact:
        resolved = [x for x in exact if x in confirmed]
        status = "resolved" if len(resolved) == len(exact) else "ambiguous"
        if status != "resolved":
            resolved = []
    else:
        # Candidate selection requires a customer discriminator, not just existence.
        resolved = confirmed if customer and len(confirmed) == 1 else []
        status = "resolved" if resolved else "ambiguous"
    if not checked or (checked and len(rejected) == len(checked)):
        status, resolved = "not_found", []
    return Result(
        task.case_id,
        task.recipient,
        task.message_id,
        {
            "entity_resolution": {
                "status": status,
                "resolved_order_ids": resolved,
                "rejected_candidates": rejected,
                "confidence": 0.9 if resolved else 0.0,
            },
            "customer_context": {"customer_unique_id": customer, "related_order_ids": related},
        },
        tuple(refs),
    )
