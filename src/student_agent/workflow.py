from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_ORDER_ID_KEYS = ("claimed_order_id", "order_id")
_CANDIDATE_KEYS = ("candidate_order_ids", "order_candidates")
_CUSTOMER_KEYS = ("customer_unique_id", "customer_id")
_TOPIC_WORKERS = {
    "shipment": ("logistics-worker",),
    "payment": ("financial-worker",),
    "refund": ("financial-worker", "policy-worker"),
    "cancellation": ("logistics-worker", "financial-worker", "policy-worker"),
    "product": ("order-product-worker", "policy-worker"),
    "unknown": ("logistics-worker", "financial-worker", "policy-worker"),
}
_TOPIC_TERMS = {
    "refund": ("refund", "refunded", "reembolso", "hoàn tiền", "hoan tien"),
    "payment": ("payment", "paid", "charge", "charged", "thanh toán", "thanh toan"),
    "shipment": (
        "shipment",
        "shipping",
        "delivery",
        "delivered",
        "late",
        "lost",
        "giao hàng",
        "giao hang",
        "vận chuyển",
        "van chuyen",
    ),
    "cancellation": ("cancel", "canceled", "cancelled", "hủy", "huy"),
    "product": ("product", "item", "seller", "sản phẩm", "san pham"),
}


def _clean_id(value: Any) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _first_value(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, "", []):
                return value[key]
        for nested in value.values():
            found = _first_value(nested, keys)
            if found not in (None, "", []):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _first_value(nested, keys)
            if found not in (None, "", []):
                return found
    return None


def _as_id_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        candidate = _clean_id(item)
        if candidate is not None and candidate not in result:
            result.append(candidate)
    return result


def _request_text(value: Any) -> str:
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, dict):
        return " ".join(f"{key} {_request_text(item)}" for key, item in value.items()).casefold()
    if isinstance(value, list):
        return " ".join(_request_text(item) for item in value).casefold()
    return str(value).casefold() if value is not None else ""


def _classify_topic(value: Any) -> str:
    text = _request_text(value)
    matches = {
        topic: sum(1 for term in terms if term in text) for topic, terms in _TOPIC_TERMS.items()
    }
    best = max(matches, key=matches.get) if matches else "unknown"
    return best if matches.get(best, 0) else "unknown"


@dataclass(frozen=True)
class Claim:
    claim_id: str
    topic: str


@dataclass(frozen=True)
class CasePlan:
    case_id: str
    claimed_order_id: str | None
    candidate_order_ids: tuple[str, ...]
    customer_unique_id: str | None
    topic: str
    claims: tuple[Claim, ...]
    workers: tuple[str, ...]


@dataclass(frozen=True)
class EntityResolution:
    status: str
    resolved_order_ids: tuple[str, ...]
    rejected_candidates: tuple[str, ...]
    confidence: float
    evidence_refs: tuple[str, ...]

    def as_output(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "resolved_order_ids": list(self.resolved_order_ids),
            "rejected_candidates": list(self.rejected_candidates),
            "confidence": self.confidence,
        }


@dataclass
class InvestigationContext:
    """Case-scoped MCP cache and evidence registry used by all workers."""

    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    _cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = field(
        default_factory=dict
    )
    evidence_refs: list[str] = field(default_factory=list)

    async def call(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]
        evidence = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
        evidence_ref = evidence["evidence_ref"]
        if evidence_ref not in self.evidence_refs:
            self.evidence_refs.append(evidence_ref)
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        self._cache[cache_key] = evidence
        return evidence


def build_case_plan(case: dict[str, Any]) -> CasePlan:
    """Normalize the request into deterministic coordinator routing data."""

    case_id = _clean_id(case.get("case_id"))
    if case_id is None:
        raise ValueError("case_id is required")
    request = case.get("customer_request", {})
    claimed = _clean_id(_first_value(request, _ORDER_ID_KEYS))
    if claimed is None:
        claimed = _clean_id(_first_value(case, ("claimed_order_id",)))
    candidates = _as_id_list(_first_value(request, _CANDIDATE_KEYS))
    if not candidates:
        candidates = _as_id_list(_first_value(case, _CANDIDATE_KEYS))
    if claimed is not None and claimed not in candidates:
        candidates.insert(0, claimed)
    customer_id = _clean_id(_first_value(request, _CUSTOMER_KEYS))
    if customer_id is None:
        customer_id = _clean_id(_first_value(case, _CUSTOMER_KEYS))

    raw_claims = _first_value(request, ("claims",))
    if raw_claims is None:
        raw_claims = case.get("claims", [])
    claims: list[Claim] = []
    if isinstance(raw_claims, list):
        for index, raw_claim in enumerate(raw_claims, start=1):
            if isinstance(raw_claim, dict):
                claim_id = _clean_id(raw_claim.get("claim_id")) or f"claim_{index}"
                topic = _clean_id(raw_claim.get("topic")) or _classify_topic(raw_claim)
            else:
                claim_id = f"claim_{index}"
                topic = _classify_topic(raw_claim)
            claims.append(Claim(claim_id=claim_id, topic=topic.casefold()))

    topic = _classify_topic(request)
    routed_topics = [topic, *(claim.topic for claim in claims)]
    workers: list[str] = ["entity-agent"]
    for routed_topic in routed_topics:
        for worker in _TOPIC_WORKERS.get(routed_topic, _TOPIC_WORKERS["unknown"]):
            if worker not in workers:
                workers.append(worker)
    workers.extend(("conflict-resolver", "verifier"))
    return CasePlan(
        case_id=case_id,
        claimed_order_id=claimed,
        candidate_order_ids=tuple(candidates),
        customer_unique_id=customer_id,
        topic=topic,
        claims=tuple(claims),
        workers=tuple(workers),
    )


def _evidence_data(evidence: dict[str, Any]) -> dict[str, Any]:
    data = evidence.get("data")
    return data if isinstance(data, dict) else {}


def _contains_value(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_value(item, expected) for item in value)
    return str(value).casefold() == expected.casefold() if value is not None else False


def _order_matches(data: dict[str, Any], order_id: str) -> bool:
    evidence_order_id = _clean_id(_first_value(data, ("order_id",)))
    return evidence_order_id == order_id if evidence_order_id is not None else bool(data)


async def resolve_entity(plan: CasePlan, context: InvestigationContext) -> EntityResolution:
    """Resolve an order using only case-scoped MCP evidence and calibrated margins."""

    context.trace.emit(
        case_id=plan.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="RESOLVE_ORDER",
        attributes={"candidate_count": len(plan.candidate_order_ids)},
    )
    if not plan.candidate_order_ids:
        result = EntityResolution("not_found", (), (), 0.0, ())
    else:
        scores: dict[str, float] = {}
        refs: dict[str, str] = {}
        for order_id in plan.candidate_order_ids:
            try:
                evidence = await context.call("entity-agent", "get_order", order_id=order_id)
            except (RuntimeError, ValueError):
                continue
            data = _evidence_data(evidence)
            if not _order_matches(data, order_id):
                continue
            score = 0.55
            if order_id == plan.claimed_order_id:
                score += 0.25
            if plan.customer_unique_id and _contains_value(data, plan.customer_unique_id):
                score += 0.15
            scores[order_id] = min(score, 0.95)
            refs[order_id] = evidence["evidence_ref"]

        ranked = sorted(
            scores, key=lambda item: (-scores[item], plan.candidate_order_ids.index(item))
        )
        if not ranked:
            result = EntityResolution("not_found", (), tuple(plan.candidate_order_ids), 0.05, ())
        else:
            winner = ranked[0]
            runner_score = scores[ranked[1]] if len(ranked) > 1 else 0.0
            margin = scores[winner] - runner_score
            is_resolved = (
                len(ranked) == 1
                or winner == plan.claimed_order_id
                or (scores[winner] >= 0.65 and margin >= 0.1)
            )
            if is_resolved:
                confidence = scores[winner]
                if len(ranked) == 1 and winner != plan.claimed_order_id:
                    confidence = max(confidence, 0.75)
                result = EntityResolution(
                    "resolved",
                    (winner,),
                    tuple(item for item in plan.candidate_order_ids if item != winner),
                    round(confidence, 2),
                    (refs[winner],),
                )
            else:
                result = EntityResolution(
                    "ambiguous",
                    tuple(ranked),
                    tuple(item for item in plan.candidate_order_ids if item not in ranked),
                    round(max(0.2, min(0.59, scores[winner] - margin)), 2),
                    tuple(refs[item] for item in ranked),
                )

    context.trace.emit(
        case_id=plan.case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code=f"ENTITY_{result.status.upper()}",
        evidence_refs=list(result.evidence_refs) or None,
        attributes={
            "confidence": result.confidence,
            "resolved_count": len(result.resolved_order_ids),
        },
    )
    return result


def merge_worker_results(*results: dict[str, Any]) -> dict[str, Any]:
    """Combine disjoint specialist payloads and reject silent key overwrites."""

    merged: dict[str, Any] = {}
    for result in results:
        overlap = merged.keys() & result.keys()
        if overlap:
            duplicated = ", ".join(sorted(overlap))
            raise ValueError(f"workers returned overlapping fields: {duplicated}")
        merged.update(result)
    return merged


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run coordinator stages implemented by Tasks 1 and 2.

    Tasks 3-7 own specialist analysis and the final schema output. Failing
    explicitly avoids emitting a plausible-looking answer without evidence.
    """

    plan = build_case_plan(case)
    context = InvestigationContext(plan.case_id, gateway, trace)
    await resolve_entity(plan, context)
    for worker in plan.workers[1:]:
        trace.emit(
            case_id=plan.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=worker,
            decision_code=f"INVESTIGATE_{re.sub(r'[^A-Z0-9]+', '_', plan.topic.upper())}",
        )
    raise NotImplementedError("Tasks 3-7 must build specialist analyses and final output")
