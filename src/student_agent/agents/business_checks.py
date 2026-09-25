from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _amount(record: dict[str, Any]) -> Decimal | None:
    for key in ("amount_brl", "amount", "value", "transaction_amount"):
        value = money(record.get(key))
        if value is not None:
            return value
    return None


def reconcile_payments(payment_data: list[dict[str, Any]], refund_data: list[dict[str, Any]]) -> dict[str, Any]:
    captures: list[Decimal] = []
    refunds: list[Decimal] = []
    capture_keys: set[str] = set()
    duplicate = False
    incomplete = False
    for row in payment_data:
        status = str(row.get("status") or row.get("payment_status") or row.get("type") or "").lower()
        amount = _amount(row)
        if amount is None:
            incomplete = True
            continue
        if any(x in status for x in ("capture", "paid", "approved", "completed")) and "refund" not in status:
            captures.append(amount)
            key = str(row.get("payment_id") or row.get("transaction_id") or row.get("authorization_code") or "")
            if key and key in capture_keys:
                duplicate = True
            if key:
                capture_keys.add(key)
    for row in refund_data:
        status = str(row.get("status") or row.get("refund_status") or "").lower()
        amount = _amount(row)
        if amount is None:
            incomplete = True
            continue
        if status in {"pending", "processing", "requested"}:
            continue
        if status in {"failed", "rejected", "denied"}:
            continue
        if status in {"refunded", "completed", "success", "succeeded"} or not status:
            refunds.append(amount)
    captured = sum(captures, Decimal(0))
    refunded = sum(refunds, Decimal(0))
    if incomplete and not captures:
        verdict = "insufficient_evidence"
    elif duplicate:
        verdict = "duplicate_capture"
    elif any(str(x.get("status") or x.get("refund_status") or "").lower() in {"failed", "rejected", "denied"} for x in refund_data):
        verdict = "refund_failed"
    elif any(str(x.get("status") or x.get("refund_status") or "").lower() in {"pending", "processing", "requested"} for x in refund_data):
        verdict = "refund_pending"
    elif refunded > 0:
        verdict = "refunded"
    else:
        verdict = "reconciled" if captures else "insufficient_evidence"
    return {"verdict": verdict, "captured": captured, "refunded": refunded, "refundable": max(captured - refunded, Decimal(0))}


def validate_business_output(output: dict[str, Any]) -> None:
    payment = output["payment_analysis"]
    if payment["captured_total_brl"] is not None and payment["refunded_total_brl"] is not None and payment["refundable_total_brl"] is not None:
        if money(payment["refundable_total_brl"]) != max(money(payment["captured_total_brl"]) - money(payment["refunded_total_brl"]), Decimal(0)):
            raise ValueError("Payment totals are inconsistent")
    financial = output["financial_resolution"]
    total = sum((money(x["amount_brl"]) or Decimal(0) for x in financial["refund_lines"]), Decimal(0))
    if money(financial["recommended_refund_brl"]) != total:
        raise ValueError("Refund lines do not sum to recommended refund")
    confidence = output["assessment"]["confidence"]
    if not 0 <= confidence <= 1:
        raise ValueError("Invalid confidence")
