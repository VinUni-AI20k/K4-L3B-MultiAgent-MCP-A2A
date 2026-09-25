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
                "logistics-worker", "get_shipment_summary", order_id=order_id
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


# =============================================================================
# TASK 4: Financial Worker
# =============================================================================


def _numbers(value: Any, keys: tuple[str, ...]) -> list[float]:
    found: list[float] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {name.casefold() for name in keys}:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    found.append(float(item))
                elif isinstance(item, str):
                    try:
                        found.append(float(item.replace(",", ".")))
                    except ValueError:
                        pass
            found.extend(_numbers(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_numbers(item, keys))
    return found


def _status_text(value: Any) -> str:
    return _request_text(value)


async def financial_worker(
    order_ids: tuple[str, ...], context: InvestigationContext
) -> dict[str, Any]:
    """Reconcile payment and refund evidence deterministically."""

    context.trace.emit(
        case_id=context.case_id, event_type="task_assigned", actor="coordinator",
        target="financial-worker", decision_code="ANALYZE_PAYMENT",
        attributes={"order_count": len(order_ids)},
    )
    captured = refunded = refundable = 0.0
    payment_seen = refund_seen = False
    pending = failed = False
    payment_refs: list[str] = []
    for order_id in order_ids:
        try:
            payment = await context.call("financial-worker", "get_order_payments", order_id=order_id)
            data = _evidence_data(payment)
            payment_seen = True
            captured += sum(_numbers(data, ("amount", "payment_value", "captured_amount", "total")))
            if payment["evidence_ref"] not in payment_refs:
                payment_refs.append(payment["evidence_ref"])
            status = _status_text(data)
            pending = pending or "pending" in status
        except (RuntimeError, ValueError):
            pass
        try:
            refund = await context.call("financial-worker", "get_refund_timeline", order_id=order_id)
            data = _evidence_data(refund)
            refund_seen = True
            amounts = _numbers(data, ("amount", "refund_amount", "refunded_amount", "total"))
            refunded += sum(amounts)
            refundable += sum(_numbers(data, ("refundable_amount", "eligible_amount")))
            if refund["evidence_ref"] not in payment_refs:
                payment_refs.append(refund["evidence_ref"])
            status = _status_text(data)
            pending = pending or "pending" in status
            failed = failed or "failed" in status or "failure" in status
        except (RuntimeError, ValueError):
            pass

    if not payment_seen and not refund_seen:
        verdict = "insufficient_evidence"
        values: tuple[float | None, float | None, float | None] = (None, None, None)
    else:
        values = (round(captured, 2), round(refunded, 2), round(refundable, 2))
        if failed:
            verdict = "refund_failed"
        elif pending:
            verdict = "refund_pending"
        elif captured > 0 and refunded > captured:
            verdict = "capture_mismatch"
        elif refunded > 0:
            verdict = "refunded"
        else:
            verdict = "reconciled"

    result = {
        "verdict": verdict,
        "captured_total_brl": values[0],
        "refunded_total_brl": values[1],
        "refundable_total_brl": values[2],
    }
    context.trace.emit(
        case_id=context.case_id, event_type="handoff", actor="financial-worker",
        target="coordinator", decision_code=f"PAYMENT_{verdict.upper()}",
        evidence_refs=payment_refs or None, attributes={"verdict": verdict},
    )
    return result


def _empty_entities(order_ids: list[str]) -> dict[str, list[str]]:
    return {"order_ids": order_ids, "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": []}


def _ids_from(value: Any, keys: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {name.casefold() for name in keys}:
                for found in _as_id_list(item):
                    if found not in result:
                        result.append(found)
            for found in _ids_from(item, keys):
                if found not in result:
                    result.append(found)
    elif isinstance(value, list):
        for item in value:
            for found in _ids_from(item, keys):
                if found not in result:
                    result.append(found)
    return result[:20]


def resolve_conflicts(
    policy: PolicyAnalysis, shipment: dict[str, Any], payment: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the public precedence policy > payment > logistics."""

    conflicts: list[dict[str, Any]] = []
    if policy.status in {"eligible", "not_eligible"} and payment.get("verdict") in {
        "refund_pending", "refund_failed", "refunded"
    }:
        conflicts.append({"field": "claim_eligibility", "sources": ["policy", "payment"],
                          "selected_source": "policy", "resolution_code": "POLICY_PRECEDENCE"})
    if shipment.get("verdict") == "conflicting":
        conflicts.append({"field": "shipment_status", "sources": ["shipment", "order"],
                          "selected_source": "shipment", "resolution_code": "SOURCE_CONFLICT"})
    causes: list[dict[str, Any]] = []
    verdict = shipment.get("verdict")
    if verdict == "seller_delay":
        causes.append({"cause_code": "SELLER_DELAY", "rank": 1})
    elif verdict == "logistics_delay":
        causes.append({"cause_code": "LOGISTICS_DELAY", "rank": 1})
    if payment.get("verdict") in {"refund_pending", "refund_failed"}:
        causes.append({"cause_code": payment["verdict"].upper(), "rank": len(causes) + 1})
    if not causes:
        causes.append({"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1})
    return conflicts[:5], {"ranked_causes": causes[:5], "responsible_parties": []}


def _primary_issue(shipment: dict[str, Any], payment: dict[str, Any], policy: PolicyAnalysis) -> str:
    if payment.get("verdict") == "refund_pending":
        return "refund_pending"
    if payment.get("verdict") == "refund_failed":
        return "refund_failed"
    if payment.get("verdict") in {"capture_mismatch", "duplicate_capture"}:
        return "payment_mismatch" if payment["verdict"] == "capture_mismatch" else "duplicate_charge"
    return {"seller_delay": "late_delivery_seller", "logistics_delay": "late_delivery_logistics"}.get(
        shipment.get("verdict"), "unsupported_claim" if policy.status == "not_eligible" else "insufficient_evidence"
    )


def build_output(
    plan: CasePlan, entity: EntityResolution, shipment: dict[str, Any],
    payment: dict[str, Any], policy: PolicyAnalysis, conflicts: list[dict[str, Any]],
    root_cause: dict[str, Any], context: InvestigationContext,
    order_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    order_data = order_data or {}
    order_ids = list(entity.resolved_order_ids)
    item_ids = _ids_from(order_data, ("item_id", "order_item_id"))
    seller_ids = _ids_from(order_data, ("seller_id", "seller"))
    shipment_ids = _ids_from(order_data, ("shipment_id", "shipping_id"))
    payment_refs = _ids_from(order_data, ("payment_id", "payment_reference", "payment_ref"))
    issue = _primary_issue(shipment, payment, policy)
    needs = entity.status != "resolved" or issue == "insufficient_evidence"
    confidence = 0.3 if needs else min(0.95, max(entity.confidence, 0.75))
    recommended_refund = float(payment.get("refundable_total_brl") or 0)
    refund_lines = ([{
        "reason_code": "POLICY_ELIGIBLE_REFUND",
        "amount_brl": round(recommended_refund, 2),
        "entity_id": order_ids[0] if order_ids else None,
    }] if recommended_refund > 0 else [])
    actions: list[str] = []
    if issue in {"refund_pending", "refund_failed", "payment_mismatch"}:
        actions.append("Review payment and refund resolution")
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        actions.append("Investigate delivery responsibility")
    if not actions and needs:
        actions.append("Request additional evidence")
    return {
        "schema_version": "day09-l3b-output-v2", "case_id": plan.case_id,
        "assessment": {"primary_issue": issue, "secondary_issues": [],
                        "case_status": "needs_investigation" if needs else "action_required",
                        "confidence": round(confidence, 2)},
        "affected_entities": _empty_entities(order_ids) | {"item_ids": item_ids, "seller_ids": seller_ids,
            "payment_references": payment_refs, "shipment_ids": shipment_ids},
        "claim_assessments": list(policy.claim_assessments),
        "entity_resolution": entity.as_output(),
        "customer_context": {"customer_unique_id": plan.customer_unique_id, "related_order_ids": order_ids},
        "shipment_analysis": shipment, "payment_analysis": payment,
        "root_cause_analysis": root_cause, "evidence_refs": list(dict.fromkeys(context.evidence_refs)),
        "data_conflicts": conflicts,
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": round(recommended_refund, 2),
                                 "refund_lines": refund_lines},
        "resolution_actions": actions,
    }


def verify_output(
    output: dict[str, Any], context: InvestigationContext
) -> dict[str, Any]:
    """Apply deterministic cross-field checks and confidence calibration.

    This verifier only changes derived fields. It never invents evidence or
    changes the server-provided evidence references.
    """

    assessment = output["assessment"]
    issue = assessment["primary_issue"]
    shipment = output["shipment_analysis"]
    payment = output["payment_analysis"]
    root = output["root_cause_analysis"]
    resolution = output["financial_resolution"]
    conflicts = output["data_conflicts"]
    evidence_count = len(output["evidence_refs"])
    quality = min(1.0, evidence_count / 4.0)
    if conflicts or shipment["verdict"] == "conflicting":
        quality *= 0.65
    if output["entity_resolution"]["status"] != "resolved":
        quality *= 0.5
    assessment["confidence"] = round(min(float(assessment["confidence"]), max(0.05, quality)), 2)

    parties = root["responsible_parties"]
    party_types = {party["party_type"] for party in parties}
    if issue == "late_delivery_seller":
        party_types.discard("logistics_provider")
        if not any(party["party_type"] == "seller" for party in parties):
            parties.append({"party_type": "unknown", "party_id": None})
    elif issue == "late_delivery_logistics":
        party_types.discard("seller")
        if not any(party["party_type"] == "logistics_provider" for party in parties):
            parties.append({"party_type": "logistics_provider", "party_id": None})
    root["responsible_parties"] = parties[:5]

    if issue in {"unsupported_claim", "insufficient_evidence"}:
        resolution["recommended_refund_brl"] = 0
        resolution["refund_lines"] = []
    elif resolution["recommended_refund_brl"] > 0 and not resolution["refund_lines"]:
        resolution["refund_lines"] = [{
            "reason_code": "VERIFIER_REFUND_RECONCILIATION",
            "amount_brl": resolution["recommended_refund_brl"],
            "entity_id": output["entity_resolution"]["resolved_order_ids"][0]
            if output["entity_resolution"]["resolved_order_ids"] else None,
        }]
    if payment["verdict"] in {"refund_pending", "refund_failed"}:
        assessment["case_status"] = "action_required"
    if not output["resolution_actions"] and assessment["case_status"] != "no_action":
        output["resolution_actions"] = ["Request additional evidence"]
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the complete case-scoped coordinator and specialist workflow."""

    plan = build_case_plan(case)
    context = InvestigationContext(plan.case_id, gateway, trace)

    # Task 1: Entity Resolution
    entity_res = await resolve_entity(plan, context)

    # Task 3: Logistics Worker
    shipment_analysis = {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False}
    if "logistics-worker" in plan.workers and entity_res.resolved_order_ids:
        shipment_analysis = await logistics_worker(entity_res.resolved_order_ids, context)

    # Task 4: Financial Worker
    payment_analysis = {"verdict": "insufficient_evidence", "captured_total_brl": None,
                        "refunded_total_brl": None, "refundable_total_brl": None}
    if "financial-worker" in plan.workers and entity_res.resolved_order_ids:
        payment_analysis = await financial_worker(entity_res.resolved_order_ids, context)

    # Task 5: Policy Worker. It consumes Task 3/4 results but does not repeat calls.
    policy = await run_policy_worker(plan, context, entity_res, shipment_analysis, payment_analysis)

    # Retrieve order evidence once more only when it was already cached by Logistics.
    order_data: dict[str, Any] = {}
    if entity_res.resolved_order_ids:
        try:
            order_data = _evidence_data(await context.call(
                "coordinator", "get_order", order_id=entity_res.resolved_order_ids[0]
            ))
        except (RuntimeError, ValueError):
            order_data = {}

    # Task 6: conflict resolver and verifier inputs.
    conflicts, root_cause = resolve_conflicts(policy, shipment_analysis, payment_analysis)
    if shipment_analysis.get("late_seller_ids"):
        root_cause["responsible_parties"] = [
            {"party_type": "seller", "party_id": seller_id}
            for seller_id in shipment_analysis["late_seller_ids"][:5]
        ]
    elif shipment_analysis.get("verdict") == "logistics_delay":
        root_cause["responsible_parties"] = [{"party_type": "logistics_provider", "party_id": None}]

    # Task 7: output builder and observable verification lifecycle.
    output = build_output(plan, entity_res, shipment_analysis, payment_analysis, policy,
                          conflicts, root_cause, context, order_data)
    output = verify_output(output, context)
    trace.emit(
        case_id=plan.case_id, event_type="verification_completed", actor="verifier",
        target="coordinator", decision_code="OUTPUT_VERIFIED",
        evidence_refs=list(context.evidence_refs),
        attributes={"evidence_count": len(context.evidence_refs), "conflict_count": len(conflicts)},
    )
    return output
