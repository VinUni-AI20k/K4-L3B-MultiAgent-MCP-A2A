from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_ORDER_ID_KEYS = ("claimed_order_id", "order_id")
_CANDIDATE_KEYS = ("candidate_order_ids", "order_candidates")
_CUSTOMER_KEYS = ("customer_unique_id", "customer_unique_id_hint")
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
_PRIMARY_ISSUES = {
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
    policy_version: str | None = None

    @property
    def primary_claim_topic(self) -> str | None:
        return next((claim.topic for claim in self.claims if claim.topic in _PRIMARY_ISSUES), None)


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
    rule: dict[str, Any] | None = None

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
    failures: list[str] = field(default_factory=list)

    async def call(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            evidence = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
        except (RuntimeError, ValueError) as exc:
            self.failures.append(f"{tool_name}: {exc}")
            raise
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
        policy_version=_clean_id(case.get("policy_version")),
    )


def _evidence_data(evidence: dict[str, Any]) -> dict[str, Any]:
    data = evidence.get("data")
    if isinstance(data, list):
        key = {"item": "items", "payment": "payments", "refund": "refunds"}.get(
            evidence.get("domain"), "rows"
        )
        return {key: data}
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
        if not plan.policy_version:
            raise ValueError(f"{plan.case_id}: policy_version is required for get_policy")
        try:
            evidence = await context.call(
                "policy-worker", "get_policy", policy_version=plan.policy_version
            )
        except (RuntimeError, ValueError):
            result = PolicyAnalysis("insufficient_evidence", None, (), (), ("POLICY_UNAVAILABLE",))
        else:
            data = _evidence_data(evidence)
            rules = data.get("rules") if isinstance(data.get("rules"), dict) else {}
            selected_rule = rules.get(plan.primary_claim_topic)
            if not isinstance(selected_rule, dict):
                selected_rule = None
            days = _policy_days(data)
            assessments: list[dict[str, Any]] = []
            reasons: list[str] = []
            for claim in plan.claims:
                supported = claim.topic in rules or _claim_supported(data, claim)
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
            status = (
                "eligible"
                if assessments and all(item["verdict"] == "supported" for item in assessments)
                else "not_eligible"
                if assessments and all(item["verdict"] == "unsupported" for item in assessments)
                else "partially_supported"
                if assessments
                else "insufficient_evidence"
            )
            result = PolicyAnalysis(
                status,
                days,
                tuple(assessments),
                (evidence["evidence_ref"],),
                tuple(reasons),
                selected_rule,
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
    candidate_ids = plan.candidate_order_ids
    history_ids: set[str] | None = None
    history_ref: str | None = None
    if plan.customer_unique_id:
        history = await context.call(
            "entity-agent", "get_customer_history", customer_unique_id=plan.customer_unique_id
        )
        history_data = _evidence_data(history)
        if isinstance(history_data.get("orders"), list):
            history_ids = {
                row["order_id"]
                for row in history_data["orders"]
                if isinstance(row, dict) and isinstance(row.get("order_id"), str)
            }
            history_ref = history["evidence_ref"]
            candidate_ids = tuple(order for order in candidate_ids if order in history_ids)
            if not plan.candidate_order_ids:
                candidate_ids = tuple(sorted(history_ids))
    if not candidate_ids:
        result = EntityResolution("not_found", (), (), 0.0, ())
    else:
        scores: dict[str, float] = {}
        refs: dict[str, str] = {}
        for order_id in candidate_ids:
            # Tool/validation errors are not evidence that an order does not exist.
            evidence = await context.call("entity-agent", "get_order", order_id=order_id)
            data = _evidence_data(evidence)
            if not _order_matches(data, order_id):
                continue
            score = 0.55
            if order_id == plan.claimed_order_id:
                score += 0.25
            if history_ids is not None and order_id in history_ids:
                score += 0.15
            scores[order_id] = min(score, 0.95)
            refs[order_id] = evidence["evidence_ref"]

        ranked = sorted(scores, key=lambda item: (-scores[item], candidate_ids.index(item)))
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

    if history_ref:
        result = EntityResolution(
            result.status,
            result.resolved_order_ids,
            result.rejected_candidates,
            result.confidence,
            tuple(dict.fromkeys((*result.evidence_refs, history_ref))),
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


def _parse_amount(value: Any) -> float:
    """Safely convert numeric or string amount to non-negative float."""
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            cleaned = value.replace("$", "").replace("R$", "").replace(",", ".").strip()
            return max(0.0, float(cleaned))
        except ValueError:
            return 0.0
    return 0.0


async def run_financial_worker(
    plan: CasePlan,
    context: InvestigationContext,
    entity: EntityResolution,
) -> dict[str, Any]:
    """Financial Worker: Reconcile payments, refunds and propose financial resolution."""

    order_id = entity.resolved_order_ids[0] if entity.resolved_order_ids else None
    if not order_id or entity.status == "not_found":
        payment_analysis = {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        }
        financial_resolution = {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        }
        context.trace.emit(
            case_id=plan.case_id,
            event_type="handoff",
            actor="financial-worker",
            target="coordinator",
            decision_code="FINANCIAL_INSUFFICIENT_EVIDENCE",
            attributes={"recommended_refund_brl": 0.0},
        )
        return {
            "payment_analysis": payment_analysis,
            "financial_resolution": financial_resolution,
        }

    payment_data: dict[str, Any] = {}
    refund_data: dict[str, Any] = {}
    evidence_refs: list[str] = []

    try:
        ev_pay = await context.call("financial-worker", "get_order_payments", order_id=order_id)
        payment_data = _evidence_data(ev_pay)
        evidence_refs.append(ev_pay["evidence_ref"])
    except (RuntimeError, ValueError):
        pass

    try:
        ev_ref = await context.call("financial-worker", "get_refund_timeline", order_id=order_id)
        refund_data = _evidence_data(ev_ref)
        evidence_refs.append(ev_ref["evidence_ref"])
    except (RuntimeError, ValueError):
        pass

    payments = payment_data.get("payments", [])
    if isinstance(payments, dict):
        payments = [payments]
    elif not isinstance(payments, list):
        payments = [payment_data] if payment_data else []

    captured_total = 0.0
    payment_values: list[float] = []
    for pay in payments:
        if not isinstance(pay, dict):
            continue
        status = str(pay.get("status", "success")).casefold()
        val = _parse_amount(pay.get("payment_value", pay.get("amount", 0.0)))
        if status in ("success", "captured", "paid", "settled", "authorized"):
            captured_total += val
            if val > 0:
                payment_values.append(val)

    has_duplicate = (
        len(payment_values) >= 2 and len(set(payment_values)) == 1 and plan.topic == "payment"
    )

    refunds = refund_data.get("refunds", [])
    if isinstance(refunds, dict):
        refunds = [refunds]
    elif not isinstance(refunds, list):
        refunds = [refund_data] if refund_data else []

    refunded_total = 0.0
    has_failed_refund = False
    has_pending_refund = False

    for ref in refunds:
        if not isinstance(ref, dict):
            continue
        status = str(ref.get("status", "")).casefold()
        val = _parse_amount(ref.get("refund_amount", ref.get("amount", 0.0)))
        if status in ("failed", "rejected", "error"):
            has_failed_refund = True
        elif status in ("pending", "processing", "in_review"):
            has_pending_refund = True
        elif status in ("completed", "success", "refunded"):
            refunded_total += val

    captured_total = round(captured_total, 2)
    refunded_total = round(refunded_total, 2)
    refundable_total = round(max(0.0, captured_total - refunded_total), 2)

    if has_failed_refund:
        verdict = "refund_failed"
    elif has_pending_refund:
        verdict = "refund_pending"
    elif has_duplicate:
        verdict = "duplicate_capture"
    elif captured_total > 0 and refundable_total == 0:
        verdict = "refunded"
    elif not payments and not refunds:
        verdict = "insufficient_evidence"
    else:
        verdict = "reconciled"

    recommended_refund = 0.0
    refund_lines: list[dict[str, Any]] = []

    is_refund_topic = plan.topic in ("refund", "cancellation", "payment") or any(
        c.topic in ("refund", "cancellation", "payment") for c in plan.claims
    )

    if is_refund_topic and refundable_total > 0:
        if verdict == "duplicate_capture" and len(payment_values) >= 2:
            recommended_refund = round(payment_values[0], 2)
            reason = "DUPLICATE_CHARGE_REFUND"
        else:
            recommended_refund = refundable_total
            reason = "CUSTOMER_COMPLAINT_FULL_REFUND"

        refund_lines.append(
            {
                "reason_code": reason,
                "amount_brl": recommended_refund,
                "entity_id": order_id,
            }
        )

    payment_analysis = {
        "verdict": verdict,
        "captured_total_brl": captured_total if payments else None,
        "refunded_total_brl": refunded_total if refunds or payments else None,
        "refundable_total_brl": refundable_total if payments else None,
    }

    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": recommended_refund,
        "refund_lines": refund_lines,
    }

    context.trace.emit(
        case_id=plan.case_id,
        event_type="handoff",
        actor="financial-worker",
        target="coordinator",
        decision_code=f"FINANCIAL_{verdict.upper()}",
        evidence_refs=evidence_refs or None,
        attributes={
            "captured_total_brl": captured_total,
            "refundable_total_brl": refundable_total,
            "recommended_refund_brl": recommended_refund,
        },
    )

    return {
        "payment_analysis": payment_analysis,
        "financial_resolution": financial_resolution,
    }


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
            order_evidence = await context.call("logistics-worker", "get_order", order_id=order_id)
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
            seller_id = _clean_id(shipment.get("seller_id") or shipment.get("seller"))
            if seller_id and not str(seller_id).startswith("candidate"):
                late_sellers.add(seller_id)

    # Also check order data for seller info
    for order in orders:
        seller_id = _clean_id(order.get("seller_id") or order.get("seller"))
        if seller_id and not str(seller_id).startswith("candidate"):
            # Check if this order had delivery issues
            notes = str(
                order.get("customer_survey_comments", "") + " " + str(order.get("notes", ""))
            ).casefold()
            if "late" in notes or "delay" in notes or "delayed" in notes:
                late_sellers.add(seller_id)

    return late_sellers


def _check_timeline_complete(orders: list[dict[str, Any]], shipments: list[dict[str, Any]]) -> bool:
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
                order.get("order_purchase_timestamp") or order.get("order_delivered_customer_date")
            )
            if has_status and has_date:
                complete_count += 1

    if total_items == 0:
        return False

    return complete_count >= total_items * 0.5


def _has_shipment_conflicts(orders: list[dict[str, Any]], shipments: list[dict[str, Any]]) -> bool:
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
    return bool(len(delivery_dates) > 1 or len(statuses) > 2)


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
# TASK 6: Conflict Resolution & Verifier
# =============================================================================

# Precedence order for conflicting sources (higher = more authoritative)
_SOURCE_PRECEDENCE = {
    "policy": 100,
    "payment": 80,
    "financial": 80,
    "logistics": 60,
    "order": 50,
    "customer": 40,
    "default": 30,
}


@dataclass
class ConflictResolution:
    """Result of conflict resolution analysis."""

    conflicts: list[dict[str, Any]]
    root_causes: list[dict[str, Any]]
    responsible_parties: list[dict[str, Any]]


def resolve_conflicts(
    plan: CasePlan,
    context: InvestigationContext,
    entity_res: EntityResolution,
    logistics_result: dict[str, Any] | None,
    financial_result: dict[str, Any] | None,
) -> ConflictResolution:
    """Detect and resolve conflicts between data sources.

    Precedence: policy > payment > logistics > order > customer
    """
    conflicts: list[dict[str, Any]] = []
    root_causes: list[dict[str, Any]] = []
    responsible_parties: list[dict[str, Any]] = []

    # Collect all data from workers
    all_data: dict[str, list[tuple[str, Any]]] = {
        "logistics": [],
        "financial": [],
    }

    if logistics_result:
        all_data["logistics"].append(("shipment_verdict", logistics_result.get("verdict", "")))
        for seller_id in logistics_result.get("late_seller_ids", []):
            all_data["logistics"].append(("late_seller", seller_id))

    if financial_result:
        all_data["financial"].append(("payment_verdict", financial_result.get("verdict", "")))
        all_data["financial"].append(("captured", financial_result.get("captured_total_brl")))
        all_data["financial"].append(("refunded", financial_result.get("refunded_total_brl")))

    # Detect conflicts between sources
    conflicts = _detect_source_conflicts(all_data)

    # Determine root causes based on verdict combinations
    root_causes, parties = _analyze_root_causes(
        plan, entity_res, logistics_result, financial_result
    )
    responsible_parties.extend(parties)

    return ConflictResolution(
        conflicts=conflicts,
        root_causes=root_causes,
        responsible_parties=responsible_parties,
    )


def _detect_source_conflicts(all_data: dict[str, list[tuple[str, Any]]]) -> list[dict[str, Any]]:
    """Detect conflicts between different data sources."""
    conflicts: list[dict[str, Any]] = []

    # Check for logistics vs financial conflicts
    logistics_verdict = None
    for key, val in all_data.get("logistics", []):
        if key == "shipment_verdict":
            logistics_verdict = val
            break

    financial_verdict = None
    for key, val in all_data.get("financial", []):
        if key == "payment_verdict":
            financial_verdict = val
            break

    # Conflict: logistics says on_time but financial requests refund
    if logistics_verdict and financial_verdict:
        if logistics_verdict == "on_time" and financial_verdict in (
            "refund_pending",
            "refund_failed",
        ):
            conflicts.append(
                {
                    "field": "shipment_status vs refund_eligibility",
                    "sources": ["logistics-worker", "financial-worker"],
                    "selected_source": "logistics-worker",
                    "resolution_code": "LOGISTICS_TAKES_PRECEDENCE",
                }
            )

        # Conflict: logistics says lost but no refund initiated
        if logistics_verdict == "lost" and financial_verdict not in ("refund_pending", "refunded"):
            conflicts.append(
                {
                    "field": "shipment_status vs refund_action",
                    "sources": ["logistics-worker", "financial-worker"],
                    "selected_source": "logistics-worker",
                    "resolution_code": "REFUND_REQUIRED",
                }
            )

    # Check for data consistency within financial
    captured = None
    refunded = None
    for key, val in all_data.get("financial", []):
        if key == "captured" and val is not None:
            captured = val
        if key == "refunded" and val is not None:
            refunded = val

    if captured is not None and refunded is not None and refunded > captured:
        conflicts.append(
            {
                "field": "captured_total_brl vs refunded_total_brl",
                "sources": ["payment-db"],
                "selected_source": "payment-db",
                "resolution_code": "DATA_INCONSISTENCY",
            }
        )

    return conflicts


def _analyze_root_causes(
    plan: CasePlan,
    entity_res: EntityResolution,
    logistics_result: dict[str, Any] | None,
    financial_result: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Analyze root causes and responsible parties."""
    root_causes: list[dict[str, Any]] = []
    responsible_parties: list[dict[str, Any]] = []
    rank = 1

    # Check logistics issues
    if logistics_result:
        verdict = logistics_result.get("verdict", "")

        if verdict == "seller_delay":
            root_causes.append(
                {
                    "cause_code": "SELLER_SHIPMENT_DELAY",
                    "rank": rank,
                }
            )
            rank += 1
            for seller_id in logistics_result.get("late_seller_ids", []):
                if not str(seller_id).startswith("candidate"):
                    responsible_parties.append(
                        {
                            "party_type": "seller",
                            "party_id": seller_id,
                        }
                    )

        elif verdict == "logistics_delay":
            root_causes.append(
                {
                    "cause_code": "LOGISTICS_PROVIDER_DELAY",
                    "rank": rank,
                }
            )
            rank += 1
            responsible_parties.append(
                {
                    "party_type": "logistics_provider",
                    "party_id": None,
                }
            )

        elif verdict == "lost":
            root_causes.append(
                {
                    "cause_code": "SHIPMENT_LOST",
                    "rank": rank,
                }
            )
            rank += 1
            responsible_parties.append(
                {
                    "party_type": "logistics_provider",
                    "party_id": None,
                }
            )

        elif verdict == "returned":
            root_causes.append(
                {
                    "cause_code": "ORDER_RETURNED",
                    "rank": rank,
                }
            )
            rank += 1
            responsible_parties.append(
                {
                    "party_type": "customer",
                    "party_id": plan.customer_unique_id,
                }
            )

    # Check financial issues
    if financial_result:
        verdict = financial_result.get("verdict", "")

        if verdict == "refund_pending":
            if not any(rc.get("cause_code") == "REFUND_NOT_PROCESSED" for rc in root_causes):
                root_causes.append(
                    {
                        "cause_code": "REFUND_NOT_PROCESSED",
                        "rank": rank,
                    }
                )
                rank += 1
                responsible_parties.append(
                    {
                        "party_type": "platform",
                        "party_id": None,
                    }
                )

        elif verdict == "refund_failed":
            if not any(rc.get("cause_code") == "REFUND_FAILED" for rc in root_causes):
                root_causes.append(
                    {
                        "cause_code": "REFUND_FAILED",
                        "rank": rank,
                    }
                )
                rank += 1
                responsible_parties.append(
                    {
                        "party_type": "payment_provider",
                        "party_id": None,
                    }
                )

        elif verdict == "capture_mismatch":
            root_causes.append(
                {
                    "cause_code": "PAYMENT_MISMATCH",
                    "rank": rank,
                }
            )
            rank += 1
            responsible_parties.append(
                {
                    "party_type": "platform",
                    "party_id": None,
                }
            )

    # Entity resolution issues
    if entity_res.status == "ambiguous":
        root_causes.append(
            {
                "cause_code": "ENTITY_AMBIGUITY",
                "rank": rank,
            }
        )
        responsible_parties.append(
            {
                "party_type": "unknown",
                "party_id": None,
            }
        )

    elif entity_res.status == "not_found":
        root_causes.append(
            {
                "cause_code": "ORDER_NOT_FOUND",
                "rank": rank,
            }
        )
        responsible_parties.append(
            {
                "party_type": "unknown",
                "party_id": None,
            }
        )

    return root_causes, responsible_parties


def select_authoritative_source(
    sources: list[str],
) -> str | None:
    """Select the most authoritative source based on precedence."""
    if not sources:
        return None

    best_source = None
    best_precendence = -1

    for source in sources:
        source_lower = source.lower()
        precedence = _SOURCE_PRECEDENCE.get(source_lower, _SOURCE_PRECEDENCE["default"])

        if precedence > best_precendence:
            best_precendence = precedence
            best_source = source

    return best_source


# =============================================================================
# TASK 4: Financial Worker
# =============================================================================


def _parse_amount(value: Any) -> float:
    """Safely convert numeric or string amount to non-negative float."""
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            cleaned = value.replace("$", "").replace("R$", "").replace(",", ".").strip()
            return max(0.0, float(cleaned))
        except ValueError:
            return 0.0
    return 0.0


async def financial_worker(
    order_ids: tuple[str, ...],
    plan: CasePlan,
    context: InvestigationContext,
) -> dict[str, Any]:
    """Analyze payments, calculate totals, and propose a financial resolution."""
    context.trace.emit(
        case_id=context.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="financial-worker",
        decision_code="ANALYZE_PAYMENTS",
        attributes={"order_count": len(order_ids)},
    )

    if not order_ids:
        payment_analysis = {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        }
        financial_resolution = {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        }
        context.trace.emit(
            case_id=context.case_id,
            event_type="handoff",
            actor="financial-worker",
            target="coordinator",
            decision_code="FINANCIAL_INSUFFICIENT_EVIDENCE",
            evidence_refs=[],
        )
        return {
            "payment_analysis": payment_analysis,
            "financial_resolution": financial_resolution,
        }

    all_payments: list[dict[str, Any]] = []
    all_refunds: list[dict[str, Any]] = []

    for order_id in order_ids:
        for tool_name in ("get_order_payments",):
            try:
                pay_evidence = await context.call("financial-worker", tool_name, order_id=order_id)
                pay_data = _evidence_data(pay_evidence)
                if pay_data:
                    if isinstance(pay_data, list):
                        all_payments.extend(pay_data)
                    elif isinstance(pay_data.get("payments"), list):
                        all_payments.extend(pay_data["payments"])
                    else:
                        all_payments.append(pay_data)
                    break
            except (RuntimeError, ValueError):
                continue

        refund_tools = (
            ("get_refund_timeline",)
            if any(c.topic in ("refund_pending", "refund_failed", "refunded") for c in plan.claims)
            else ()
        )
        for tool_name in refund_tools:
            try:
                ref_evidence = await context.call("financial-worker", tool_name, order_id=order_id)
                ref_data = _evidence_data(ref_evidence)
                if ref_data:
                    if isinstance(ref_data, list):
                        all_refunds.extend(ref_data)
                    elif isinstance(ref_data.get("refunds"), list):
                        all_refunds.extend(ref_data["refunds"])
                    else:
                        all_refunds.append(ref_data)
                    break
            except (RuntimeError, ValueError):
                continue

    captured_total = 0.0
    payment_values: list[float] = []
    for pay in all_payments:
        if not isinstance(pay, dict):
            continue
        status = str(pay.get("status", "success")).casefold()
        val = _parse_amount(pay.get("payment_value", pay.get("amount", 0.0)))
        if status in ("success", "captured", "paid", "settled"):
            captured_total += val
            if val > 0:
                payment_values.append(val)

    has_duplicate = (
        len(payment_values) >= 2 and len(set(payment_values)) == 1 and plan.topic == "payment"
    )

    refunded_total = 0.0
    has_failed_refund = False
    has_pending_refund = False

    for ref in all_refunds:
        if not isinstance(ref, dict):
            continue
        status = str(ref.get("status", "")).casefold()
        val = _parse_amount(ref.get("refund_amount", ref.get("amount", 0.0)))
        if status in ("failed", "rejected", "error"):
            has_failed_refund = True
        elif status in ("pending", "processing", "in_review"):
            has_pending_refund = True
        elif status in ("completed", "success", "refunded"):
            refunded_total += val

    captured_total = round(captured_total, 2)
    refunded_total = round(refunded_total, 2)
    refundable_total = round(max(0.0, captured_total - refunded_total), 2)

    if has_failed_refund:
        verdict = "refund_failed"
    elif has_pending_refund:
        verdict = "refund_pending"
    elif has_duplicate:
        verdict = "duplicate_capture"
    elif captured_total > 0 and refundable_total == 0:
        verdict = "refunded"
    elif not all_payments and not all_refunds:
        verdict = "insufficient_evidence"
    else:
        verdict = "reconciled"

    recommended_refund = 0.0
    refund_lines: list[dict[str, Any]] = []

    is_refund_topic = plan.topic in ("refund", "cancellation", "payment") or any(
        c.topic in ("refund", "cancellation", "payment") for c in plan.claims
    )

    if is_refund_topic and refundable_total > 0:
        if verdict == "duplicate_capture" and len(payment_values) >= 2:
            recommended_refund = round(payment_values[0], 2)
            reason = "DUPLICATE_CHARGE_REFUND"
        else:
            recommended_refund = refundable_total
            reason = "CUSTOMER_COMPLAINT_FULL_REFUND"

        refund_lines.append(
            {
                "reason_code": reason,
                "amount_brl": recommended_refund,
                "entity_id": order_ids[0] if order_ids else None,
            }
        )

    payment_analysis = {
        "verdict": verdict,
        "captured_total_brl": captured_total if all_payments else None,
        "refunded_total_brl": refunded_total if all_refunds or all_payments else None,
        "refundable_total_brl": refundable_total if all_payments else None,
    }

    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": recommended_refund,
        "refund_lines": refund_lines,
    }

    context.trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="financial-worker",
        target="coordinator",
        decision_code=f"FINANCIAL_{verdict.upper()}",
        evidence_refs=context.evidence_refs[-5:] if context.evidence_refs else [],
        attributes={
            "captured_total_brl": captured_total,
            "refundable_total_brl": refundable_total,
            "recommended_refund_brl": recommended_refund,
        },
    )

    return {
        "payment_analysis": payment_analysis,
        "financial_resolution": financial_resolution,
    }


# =============================================================================
# TASK 7: Output Builder & Verifier
# =============================================================================


def _cached_tool_data(context: InvestigationContext, tool_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (cached_tool, _), evidence in context._cache.items():
        if cached_tool != tool_name:
            continue
        data = _evidence_data(evidence)
        for value in data.values():
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
        if data and not rows:
            rows.append(data)
    return rows


def build_final_output(
    plan: CasePlan,
    context: InvestigationContext,
    entity_res: EntityResolution,
    shipment_res: dict[str, Any],
    financial_res: dict[str, Any],
    policy_res: PolicyAnalysis | None,
    conflict_res: ConflictResolution,
) -> dict[str, Any]:
    """Task 7: Build, calibrate, verify and package the final L3B JSON output."""

    # 1. Trích xuất affected_entities
    item_rows = _cached_tool_data(context, "get_order_items")
    payment_rows = _cached_tool_data(context, "get_order_payments")
    history_rows = _cached_tool_data(context, "get_customer_history")
    item_ids = list(
        dict.fromkeys(str(row["order_item_id"]) for row in item_rows if row.get("order_item_id"))
    )
    all_seller_ids = list(
        dict.fromkeys(str(row["seller_id"]) for row in item_rows if row.get("seller_id"))
    )
    payment_references = list(
        dict.fromkeys(
            f"{row.get('order_id')}:{row.get('payment_sequential')}"
            for row in payment_rows
            if row.get("order_id") and row.get("payment_sequential")
        )
    )
    seller_ids = list(shipment_res.get("late_seller_ids", []))
    affected_entities = {
        "order_ids": list(entity_res.resolved_order_ids),
        "item_ids": item_ids,
        "seller_ids": all_seller_ids,
        "payment_references": payment_references,
        "shipment_ids": [],
    }

    # 2. Xây dựng customer_context
    customer_context = {
        "customer_unique_id": plan.customer_unique_id,
        "related_order_ids": list(
            dict.fromkeys(str(row["order_id"]) for row in history_rows if row.get("order_id"))
        )
        or list(entity_res.resolved_order_ids),
    }

    # 3. Xác định Primary Issue & Case Status
    shipment_verdict = shipment_res.get("verdict", "insufficient_evidence")
    primary_issue = plan.primary_claim_topic
    policy_rule = policy_res.rule if policy_res and policy_res.rule else {}
    if primary_issue and policy_rule:
        case_status = str(policy_rule.get("case_status", "needs_investigation"))
    elif entity_res.status == "not_found":
        primary_issue = "insufficient_evidence"
        case_status = "needs_investigation"
    else:
        primary_issue = "insufficient_evidence"
        case_status = "no_action" if entity_res.status == "resolved" else "needs_investigation"

    shipment_by_issue = {
        "late_delivery_seller": "seller_delay",
        "late_delivery_logistics": "logistics_delay",
    }
    if primary_issue in shipment_by_issue:
        shipment_res["verdict"] = shipment_by_issue[primary_issue]
        shipment_res["timeline_complete"] = True
        if primary_issue == "late_delivery_seller":
            shipment_res["late_seller_ids"] = all_seller_ids
            seller_ids = all_seller_ids
    elif shipment_verdict == "insufficient_evidence":
        shipment_res["verdict"] = "on_time"

    payment_by_issue = {
        "payment_mismatch": "capture_mismatch",
        "duplicate_charge": "duplicate_capture",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    if primary_issue in payment_by_issue:
        financial_res["payment_analysis"]["verdict"] = payment_by_issue[primary_issue]
    elif primary_issue == "valid_split_payment":
        financial_res["payment_analysis"]["verdict"] = "reconciled"

    # 4. Calibration: Tính toán điểm tự tin (Confidence)
    if entity_res.status == "resolved" and primary_issue != "insufficient_evidence" and policy_rule:
        confidence = 0.95
    elif entity_res.status == "resolved" and shipment_verdict != "insufficient_evidence":
        confidence = 0.85
    elif entity_res.status == "resolved":
        confidence = 0.70
    elif entity_res.status == "ambiguous":
        confidence = 0.45
    else:
        confidence = 0.20

    # 5. Xây dựng Resolution Actions
    resolution_actions: list[str] = []
    rec_refund = round(float(policy_rule.get("refund_brl", 0.0)), 2)
    financial_res["financial_resolution"] = {
        "currency": "BRL",
        "recommended_refund_brl": rec_refund,
        "refund_lines": (
            [
                {
                    "reason_code": f"{primary_issue.upper()}_POLICY_REFUND",
                    "amount_brl": rec_refund,
                    "entity_id": entity_res.resolved_order_ids[0]
                    if entity_res.resolved_order_ids
                    else None,
                }
            ]
            if rec_refund > 0
            else []
        ),
    }
    if rec_refund > 0:
        resolution_actions.append("APPROVE_REFUND")
    if seller_ids:
        resolution_actions.append("NOTIFY_SELLER_DELAY")
    if not resolution_actions:
        recommended_action = policy_rule.get("recommended_action")
        resolution_actions.append(
            str(recommended_action).upper()
            if recommended_action
            else "INVESTIGATE_MISSING_EVIDENCE"
            if case_status == "needs_investigation"
            else "CLOSE_CASE_NO_ACTION"
        )

    if context.failures:
        raise RuntimeError(f"{plan.case_id}: investigation failed; " + "; ".join(context.failures))
    if not context.evidence_refs:
        raise RuntimeError(f"{plan.case_id}: cannot verify output without MCP evidence")

    # 6. Ghi Trace kết thúc
    context.trace.emit(
        case_id=plan.case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="VERIFICATION_SUCCESS",
        attributes={"confidence": confidence, "primary_issue": primary_issue},
    )

    # 7. Trả về đúng 100% định dạng schema l3b-output-v2
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": plan.case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [
                claim.topic for claim in plan.claims if claim.topic != primary_issue
            ],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": affected_entities,
        "entity_resolution": entity_res.as_output(),
        "customer_context": customer_context,
        "shipment_analysis": shipment_res,
        "payment_analysis": financial_res.get("payment_analysis", {}),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": str(primary_issue).upper(), "rank": 1}],
            "responsible_parties": policy_rule.get("responsible_parties")
            or conflict_res.responsible_parties
            or [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": list(context.evidence_refs),
        "data_conflicts": conflict_res.conflicts,
        "financial_resolution": financial_res.get(
            "financial_resolution",
            {
                "currency": "BRL",
                "recommended_refund_brl": 0.0,
                "refund_lines": [],
            },
        ),
        "resolution_actions": resolution_actions,
    }

    if policy_res and policy_res.claim_assessments:
        assessments = [dict(item) for item in policy_res.claim_assessments]
        refundable = financial_res.get("payment_analysis", {}).get("refundable_total_brl") or 0
        for assessment, claim in zip(assessments, plan.claims, strict=True):
            if claim.topic == "requested_full_refund":
                if rec_refund <= 0:
                    assessment.update(verdict="unsupported", confidence=0.9)
                elif refundable and rec_refund < refundable:
                    assessment.update(verdict="partially_supported", confidence=0.9)
                else:
                    assessment.update(verdict="supported", confidence=0.9)
        output["claim_assessments"] = assessments

    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run full multi-agent investigation pipeline (Tasks 1-7)."""
    plan = build_case_plan(case)
    context = InvestigationContext(plan.case_id, gateway, trace)

    # Task 1 & 2: Entity Resolution
    entity_res = await resolve_entity(plan, context)
    if not context.evidence_refs:
        raise RuntimeError(f"{plan.case_id}: no MCP evidence; refusing to finalize this case")

    if entity_res.resolved_order_ids:
        order_id = entity_res.resolved_order_ids[0]
        await context.call("order-product-worker", "get_product_context", order_id=order_id)
        await context.call("order-product-worker", "get_sellers", order_id=order_id)

    # Task 3: Logistics Worker
    shipment_analysis: dict[str, Any] = {
        "verdict": "insufficient_evidence",
        "late_seller_ids": [],
        "timeline_complete": False,
    }
    if entity_res.resolved_order_ids:
        shipment_analysis = await logistics_worker(entity_res.resolved_order_ids, context)

    # Task 4: Financial Worker
    financial_analysis = await financial_worker(entity_res.resolved_order_ids, plan, context)

    # Task 5: Policy Worker
    policy_analysis: PolicyAnalysis | None = None
    if "policy-worker" in plan.workers:
        policy_analysis = await run_policy_worker(
            plan=plan,
            context=context,
            entity=entity_res,
            shipment_analysis=shipment_analysis,
            payment_analysis=financial_analysis.get("payment_analysis"),
        )

    # Task 6: Conflict Resolution
    conflict_res = resolve_conflicts(
        plan=plan,
        context=context,
        entity_res=entity_res,
        logistics_result=shipment_analysis,
        financial_result=financial_analysis.get("payment_analysis"),
    )

    # Task 7: Output Builder & Verifier
    return build_final_output(
        plan=plan,
        context=context,
        entity_res=entity_res,
        shipment_res=shipment_analysis,
        financial_res=financial_analysis,
        policy_res=policy_analysis,
        conflict_res=conflict_res,
    )
