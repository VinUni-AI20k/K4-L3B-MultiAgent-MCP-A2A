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


@dataclass(frozen=True)
class PolicyAnalysis:
    """Evidence-backed policy decision returned by the policy worker."""

    status: str
    applicable_days: int | None
    claim_assessments: tuple[dict[str, Any], ...]
    evidence_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def as_output(self) -> dict[str, Any]:
        return {
            "policy_status": self.status,
            "applicable_days": self.applicable_days,
            "claim_assessments": [dict(item) for item in self.claim_assessments],
            "evidence_refs": list(self.evidence_refs),
            "reason_codes": list(self.reason_codes),
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


def _first_scalar(value: Any, keys: tuple[str, ...]) -> Any:
    """Find a scalar policy attribute in differently shaped gateway payloads."""

    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float, bool)):
                return candidate
        for nested in value.values():
            found = _first_scalar(nested, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _first_scalar(nested, keys)
            if found is not None:
                return found
    return None


def _policy_days(data: dict[str, Any]) -> int | None:
    raw = _first_scalar(
        data,
        ("window_days", "validity_days", "eligible_days", "deadline_days", "days"),
    )
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None
    return days if days in (7, 30) else days if days > 0 else None


def _claim_supported(policy_data: dict[str, Any], claim: Claim) -> bool | None:
    """Match a claim against explicit policy fields without guessing missing data."""

    text = _request_text(policy_data)
    topic = claim.topic.casefold()
    positive = ("eligible", "covered", "allowed", "valid", "applies", "supported")
    negative = ("ineligible", "excluded", "not covered", "not allowed", "invalid")
    if topic and topic in text:
        if any(term in text for term in negative):
            return False
        if any(term in text for term in positive):
            return True
    return None


async def run_policy_worker(
    plan: CasePlan,
    context: InvestigationContext,
    entity: EntityResolution,
    shipment_analysis: dict[str, Any] | None = None,
    payment_analysis: dict[str, Any] | None = None,
) -> PolicyAnalysis:
    """Evaluate claims using the policy MCP source and task 3/4 observations.

    Task 3 and 4 results are context for policy validation only. The worker does
    not duplicate their MCP calls, and missing observations remain unresolved.
    """

    context.trace.emit(
        case_id=plan.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-worker",
        decision_code="CHECK_POLICY",
        attributes={"claim_count": len(plan.claims)},
    )
    if entity.status != "resolved" or len(entity.resolved_order_ids) != 1:
        result = PolicyAnalysis("insufficient_evidence", None, (), (), ("ENTITY_UNRESOLVED",))
    else:
        order_id = entity.resolved_order_ids[0]
        try:
            evidence = await context.call("policy-worker", "get_policy", order_id=order_id)
        except (RuntimeError, ValueError):
            result = PolicyAnalysis("insufficient_evidence", None, (), (), ("POLICY_UNAVAILABLE",))
        else:
            data = _evidence_data(evidence)
            days = _policy_days(data)
            assessments: list[dict[str, Any]] = []
            reasons: list[str] = []
            for claim in plan.claims:
                supported = _claim_supported(data, claim)
                if supported is True:
                    verdict, confidence = "supported", 0.85
                elif supported is False:
                    verdict, confidence = "unsupported", 0.85
                else:
                    verdict, confidence = "insufficient_evidence", 0.3
                assessments.append(
                    {
                        "claim_id": claim.claim_id,
                        "verdict": verdict,
                        "confidence": confidence,
                        "evidence_refs": [evidence["evidence_ref"]],
                    }
                )
                reasons.append(f"{claim.claim_id}:{verdict.upper()}")

            # These observations are deliberately consulted, not overridden: the
            # policy source remains authoritative for eligibility and windows.
            if days is None:
                reasons.append("POLICY_WINDOW_UNSPECIFIED")
            if shipment_analysis is not None and not shipment_analysis:
                reasons.append("SHIPMENT_ANALYSIS_EMPTY")
            if payment_analysis is not None and not payment_analysis:
                reasons.append("PAYMENT_ANALYSIS_EMPTY")
            status = "eligible" if assessments and all(
                item["verdict"] == "supported" for item in assessments
            ) else "not_eligible" if assessments and all(
                item["verdict"] == "unsupported" for item in assessments
            ) else "partially_supported" if assessments else "insufficient_evidence"
            result = PolicyAnalysis(
                status, days, tuple(assessments), (evidence["evidence_ref"],), tuple(reasons)
            )

    context.trace.emit(
        case_id=plan.case_id,
        event_type="policy_decided",
        actor="policy-worker",
        target="coordinator",
        decision_code=result.status.upper(),
        evidence_refs=list(result.evidence_refs) or None,
        attributes={"applicable_days": result.applicable_days},
    )
    return result


policy_worker = run_policy_worker


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


# =============================================================================
# TASK 3: Logistics Worker
# =============================================================================


async def logistics_worker(
    order_ids: tuple[str, ...], context: InvestigationContext
) -> dict[str, Any]:
    """Analyze shipment status and determine delivery verdict.

    Verdict options:
    - on_time: Giao đúng hạn
    - seller_delay: Seller gửi trễ
    - logistics_delay: logistics giao trễ
    - lost: Mất hàng
    - returned: Hoàn hàng
    - conflicting: Dữ liệu mâu thuẫn
    - insufficient_evidence: Không đủ bằng chứng
    """
    context.trace.emit(
        case_id=context.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="logistics-worker",
        decision_code="ANALYZE_SHIPMENT",
        attributes={"order_count": len(order_ids)},
    )

    all_orders: list[dict[str, Any]] = []
    all_shipments: list[dict[str, Any]] = []
    late_seller_ids: set[str] = set()
    has_data = False

    for order_id in order_ids:
        # Get order details
        try:
            order_evidence = await context.call(
                "logistics-worker", "get_order", order_id=order_id
            )
            order_data = _evidence_data(order_evidence)
            if order_data:
                all_orders.append(order_data)
                has_data = True
        except (RuntimeError, ValueError):
            pass

        # Get order items (for seller info)
        try:
            items_evidence = await context.call(
                "logistics-worker", "get_order_items", order_id=order_id
            )
            items_data = _evidence_data(items_evidence)
            if items_data:
                # Merge seller info into orders
                for order in all_orders:
                    if order.get("order_id") == order_id:
                        if "seller_id" not in order and "seller_id" in items_data:
                            order["seller_id"] = items_data.get("seller_id")
                        break
        except (RuntimeError, ValueError):
            pass

        # Get shipment details
        try:
            shipment_evidence = await context.call(
                "logistics-worker", "get_shipment", order_id=order_id
            )
            shipment_data = _evidence_data(shipment_evidence)
            if shipment_data:
                all_shipments.append(shipment_data)
                has_data = True
        except (RuntimeError, ValueError):
            pass

    if not has_data:
        context.trace.emit(
            case_id=context.case_id,
            event_type="handoff",
            actor="logistics-worker",
            target="coordinator",
            decision_code="SHIPMENT_INSUFFICIENT_EVIDENCE",
            evidence_refs=[],
        )
        return {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        }

    # Determine verdict
    verdict = _determine_shipment_verdict(all_orders, all_shipments)

    # Extract late sellers
    late_seller_ids = _extract_late_sellers(all_orders, all_shipments)

    # Check timeline completeness
    timeline_complete = _check_timeline_complete(all_orders, all_shipments)

    # Check for conflicts
    if _has_shipment_conflicts(all_orders, all_shipments):
        verdict = "conflicting"

    context.trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="logistics-worker",
        target="coordinator",
        decision_code=f"SHIPMENT_{verdict.upper()}",
        evidence_refs=context.evidence_refs[-5:] if context.evidence_refs else [],
        attributes={
            "verdict": verdict,
            "late_seller_count": len(late_seller_ids),
            "timeline_complete": timeline_complete,
        },
    )

    return {
        "verdict": verdict,
        "late_seller_ids": list(late_seller_ids),
        "timeline_complete": timeline_complete,
    }


def _determine_shipment_verdict(
    orders: list[dict[str, Any]], shipments: list[dict[str, Any]]
) -> str:
    """Determine shipment verdict based on order and shipment data."""
    if not orders and not shipments:
        return "insufficient_evidence"

    # Check for returned status
    for shipment in shipments:
        status = str(shipment.get("status", "")).casefold()
        shipping_status = str(shipment.get("shipping_status", "")).casefold()
        if "return" in status or "return" in shipping_status:
            return "returned"

    # Check for lost shipment
    for shipment in shipments:
        status = str(shipment.get("status", "")).casefold()
        shipping_status = str(shipment.get("shipping_status", "")).casefold()
        delivery_status = str(shipment.get("delivery_status", "")).casefold()
        if "lost" in status or "lost" in shipping_status or "lost" in delivery_status:
            return "lost"

    # Analyze delivery timing
    on_time_count = 0
    late_count = 0
    unknown_count = 0

    for shipment in shipments:
        promised = _parse_date(
            shipment.get("shipping_limit_date")
            or shipment.get("promise_date")
            or shipment.get("estimated_delivery_date")
        )
        delivered = _parse_date(
            shipment.get("delivery_date")
            or shipment.get("actual_delivery_date")
            or shipment.get("delivered_date")
        )

        if promised and delivered:
            if delivered <= promised:
                on_time_count += 1
            else:
                late_count += 1
        elif promised and not delivered:
            status = str(shipment.get("status", "")).casefold()
            if "delivered" not in status and "complete" not in status:
                # Still pending, but check if past promise date
                unknown_count += 1
            else:
                late_count += 1
        else:
            unknown_count += 1

    # Fallback to order status if no shipment data
    if not shipments and orders:
        for order in orders:
            status = str(order.get("order_status", "")).casefold()
            if "delivered" in status:
                on_time_count += 1
            elif "shipped" in status or "processing" in status:
                unknown_count += 1

    total = on_time_count + late_count + unknown_count
    if total == 0:
        return "insufficient_evidence"

    if late_count > 0 and on_time_count == 0 and unknown_count == 0:
        return "seller_delay"

    if late_count > 0 and unknown_count == 0:
        return "logistics_delay"

    if late_count > 0 and unknown_count > 0:
        return "conflicting"

    if unknown_count > 0 and late_count == 0 and on_time_count == 0:
        return "insufficient_evidence"

    return "on_time"


def _extract_late_sellers(
    orders: list[dict[str, Any]], shipments: list[dict[str, Any]]
) -> set[str]:
    """Extract seller IDs responsible for late delivery."""
    late_sellers: set[str] = set()

    for shipment in shipments:
        promised = _parse_date(
            shipment.get("shipping_limit_date")
            or shipment.get("promise_date")
            or shipment.get("estimated_delivery_date")
        )
        delivered = _parse_date(
            shipment.get("delivery_date")
            or shipment.get("actual_delivery_date")
            or shipment.get("delivered_date")
        )

        if promised and delivered and delivered > promised:
            seller_id = _clean_id(
                shipment.get("seller_id")
                or shipment.get("seller")
            )
            if seller_id and not str(seller_id).startswith("candidate"):
                late_sellers.add(seller_id)

    # Also check order data for seller info
    for order in orders:
        seller_id = _clean_id(order.get("seller_id") or order.get("seller"))
        if seller_id and not str(seller_id).startswith("candidate"):
            # Check if this order had delivery issues
            status = str(order.get("order_status", "")).casefold()
            notes = str(
                order.get("customer_survey_comments", "") + " " + str(order.get("notes", ""))
            ).casefold()
            if ("late" in notes or "delay" in notes or "delayed" in notes):
                late_sellers.add(seller_id)

    return late_sellers


def _check_timeline_complete(
    orders: list[dict[str, Any]], shipments: list[dict[str, Any]]
) -> bool:
    """Check if shipment timeline has complete information."""
    if not shipments and not orders:
        return False

    complete_count = 0
    total_items = max(len(shipments), len(orders))

    for shipment in shipments:
        has_status = bool(shipment.get("status") or shipment.get("shipping_status"))
        has_delivery = bool(
            shipment.get("delivery_date")
            or shipment.get("actual_delivery_date")
            or shipment.get("delivered_date")
        )
        has_promise = bool(
            shipment.get("shipping_limit_date")
            or shipment.get("promise_date")
            or shipment.get("estimated_delivery_date")
        )

        if has_status and (has_delivery or has_promise):
            complete_count += 1

    # Check orders if no shipment data
    if not shipments:
        for order in orders:
            has_status = bool(order.get("order_status"))
            has_date = bool(
                order.get("order_purchase_timestamp")
                or order.get("order_delivered_customer_date")
            )
            if has_status and has_date:
                complete_count += 1

    if total_items == 0:
        return False

    return complete_count >= total_items * 0.5


def _has_shipment_conflicts(
    orders: list[dict[str, Any]], shipments: list[dict[str, Any]]
) -> bool:
    """Check for conflicting evidence in shipment data."""
    if len(shipments) < 2:
        return False

    delivery_dates: set[str] = set()
    for shipment in shipments:
        delivered = _parse_date(
            shipment.get("delivery_date")
            or shipment.get("actual_delivery_date")
            or shipment.get("delivered_date")
        )
        if delivered:
            delivery_dates.add(str(delivered.date()))

    statuses: set[str] = set()
    for shipment in shipments:
        status = str(shipment.get("status", "")).casefold()
        if status:
            statuses.add(status)

    # Multiple different delivery dates or many conflicting statuses indicate conflict
    if len(delivery_dates) > 1 or len(statuses) > 2:
        return True

    return False


def _parse_date(value: Any) -> Any:
    """Parse date string to comparable datetime object."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value
    if isinstance(value, str) and value:
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
        ):
            try:
                from datetime import datetime

                return datetime.strptime(value[:19], fmt)
            except ValueError:
                continue
    return None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run coordinator stages: Entity Resolution + Logistics Worker.

    Tasks 4-7 must build remaining specialist analyses and final schema output.
    """

    plan = build_case_plan(case)
    context = InvestigationContext(plan.case_id, gateway, trace)

    # Task 1: Entity Resolution
    entity_res = await resolve_entity(plan, context)

    # Task 3: Logistics Worker
    shipment_analysis = {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False}
    if "logistics-worker" in plan.workers and entity_res.resolved_order_ids:
        shipment_analysis = await logistics_worker(entity_res.resolved_order_ids, context)

    # Tasks 4-7 must build: financial_worker, policy_worker, conflict_resolver, verifier, final output
    for worker in plan.workers[1:]:
        trace.emit(
            case_id=plan.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=worker,
            decision_code=f"INVESTIGATE_{re.sub(r'[^A-Z0-9]+', '_', plan.topic.upper())}",
        )
    raise NotImplementedError("Tasks 4-7 must build specialist analyses and final output")
