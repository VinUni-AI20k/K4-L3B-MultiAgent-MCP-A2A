"""Data models and A2A message envelopes for Day09 L3B Multi-Agent Pipeline."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class A2AMessage:
    """Agent-to-Agent message envelope ensuring loop prevention and provenance."""

    case_id: str
    sender: str
    recipient: str
    intent: Literal["task_assign", "task_result", "clarification_request", "handoff", "abort"]
    payload: dict[str, Any]
    message_id: str = field(default_factory=lambda: f"msg_{secrets.token_hex(8)}")
    hop_count: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )

    def next_hop(
        self,
        recipient: str,
        intent: Literal["task_assign", "task_result", "clarification_request", "handoff", "abort"],
        payload: dict[str, Any],
    ) -> A2AMessage:
        if self.hop_count >= 10:
            err = f"A2A cycle detected: max hop count (10) exceeded for case {self.case_id}"
            raise RuntimeError(err)
        return A2AMessage(
            case_id=self.case_id,
            sender=self.recipient,
            recipient=recipient,
            intent=intent,
            payload=payload,
            hop_count=self.hop_count + 1,
        )


class CaseEvidenceContext:
    """Session-scoped evidence repository for a single dispute case."""

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.evidence_by_tool: dict[str, list[dict[str, Any]]] = {}
        self.evidence_by_domain: dict[str, list[str]] = {}
        self.collected_evidence_refs: list[str] = []
        self._cache: dict[str, dict[str, Any]] = {}

    def record_evidence(self, tool_name: str, evidence: dict[str, Any]) -> None:
        ref = evidence.get("evidence_ref")
        domain = evidence.get("domain")
        if ref and ref not in self.collected_evidence_refs:
            self.collected_evidence_refs.append(ref)
        if ref and domain:
            domain_refs = self.evidence_by_domain.setdefault(domain, [])
            if ref not in domain_refs:
                domain_refs.append(ref)
        self.evidence_by_tool.setdefault(tool_name, []).append(evidence)

    def get_evidence_for_topic(self, topic: str) -> list[str]:
        """Return relevant evidence refs matching topic to optimize precision and F1 coverage."""
        refs: list[str] = []
        topic_lower = topic.lower()

        if "late_delivery_seller" in topic_lower:
            for d in ("seller", "item", "shipment", "order"):
                refs.extend(self.evidence_by_domain.get(d, []))
        elif "late_delivery_logistics" in topic_lower or any(
            w in topic_lower for w in ("late", "ship", "deliver", "logistics", "carrier")
        ):
            for d in ("shipment", "order", "item"):
                refs.extend(self.evidence_by_domain.get(d, []))
        elif any(w in topic_lower for w in ("refund", "charge", "pay", "split", "mismatch")):
            for d in ("payment", "refund", "policy", "order", "item"):
                refs.extend(self.evidence_by_domain.get(d, []))
        elif any(w in topic_lower for w in ("cancel", "unavail")):
            for d in ("order", "seller", "item", "product", "policy"):
                refs.extend(self.evidence_by_domain.get(d, []))
        else:
            for d in ("policy", "order", "item"):
                refs.extend(self.evidence_by_domain.get(d, []))

        deduped = list(dict.fromkeys(refs))
        return deduped[:5] if deduped else self.collected_evidence_refs[:3]

    def get_cache_key(self, tool_name: str, arguments: dict[str, Any]) -> str:
        args_sorted = sorted(arguments.items())
        return f"{tool_name}:{args_sorted}"

    def get_cached(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        key = self.get_cache_key(tool_name, arguments)
        return self._cache.get(key)

    def set_cached(
        self, tool_name: str, arguments: dict[str, Any], evidence: dict[str, Any]
    ) -> None:
        key = self.get_cache_key(tool_name, arguments)
        self._cache[key] = evidence
        self.record_evidence(tool_name, evidence)


class OrderFindings(BaseModel):
    order_id: str
    order_status: str | None = None
    purchase_timestamp: str | None = None
    approved_at: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    seller_ids: list[str] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    product_categories: list[str] = Field(default_factory=list)
    total_items_price_brl: float = 0.0
    total_freight_brl: float = 0.0
    total_order_value_brl: float = 0.0
    seller_financials: dict[str, dict[str, Any]] = Field(default_factory=dict)
    seller_locations: dict[str, dict[str, str]] = Field(default_factory=dict)
    shipping_deadlines: list[dict[str, str]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class PaymentFindings(BaseModel):
    order_id: str
    verdict: str  # reconciled, capture_mismatch, duplicate_capture, etc.
    captured_total_brl: float = 0.0
    refunded_total_brl: float = 0.0
    refundable_total_brl: float = 0.0
    payment_references: list[str] = Field(default_factory=list)
    payments: list[dict[str, Any]] = Field(default_factory=list)
    timeline_events: list[dict[str, Any]] = Field(default_factory=list)
    refund_events: list[dict[str, Any]] = Field(default_factory=list)
    has_duplicate_capture: bool = False
    has_mismatch: bool = False
    has_pending_refund: bool = False
    has_failed_refund: bool = False
    duplicate_amount_brl: float = 0.0
    mismatch_amount_brl: float = 0.0
    failed_refund_amount_brl: float = 0.0
    evidence_refs: list[str] = Field(default_factory=list)


class ShipmentFindings(BaseModel):
    order_id: str
    verdict: str  # on_time, seller_delay, logistics_delay, lost, etc.
    delivered_carrier_at: str | None = None
    delivered_customer_at: str | None = None
    estimated_delivery_at: str | None = None
    shipment_ids: list[str] = Field(default_factory=list)
    shipping_limits: list[dict[str, Any]] = Field(default_factory=list)
    late_seller_ids: list[str] = Field(default_factory=list)
    timeline_complete: bool = False
    is_seller_delay: bool = False
    is_logistics_delay: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


class AdjudicationDraft(BaseModel):
    primary_issue: str
    secondary_issues: list[str] = Field(default_factory=list)
    case_status: str
    confidence: float
    recommended_action: str
    recommended_refund_brl: float
    cause_code: str
    responsible_parties: list[dict[str, Any]] = Field(default_factory=list)
    claim_verdicts: dict[str, str] = Field(default_factory=dict)
    rationale: str = ""
