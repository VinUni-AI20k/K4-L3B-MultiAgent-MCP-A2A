"""Order and Product Specialist Agent."""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .models import CaseEvidenceContext, OrderFindings


class OrderAgent:
    """Specialist responsible for order status, item inventory,
    seller details and catalog context.
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self, case_id: str, order_id: str, scope: dict[str, Any], context: CaseEvidenceContext
    ) -> OrderFindings:
        evidence_refs: list[str] = []
        findings = OrderFindings(order_id=order_id)

        # 1. get_order
        try:
            cached_order = context.get_cached("get_order", {"order_id": order_id})
            if cached_order is None:
                order_ev = await self.gateway.call("get_order", case_id=case_id, order_id=order_id)
                context.set_cached("get_order", {"order_id": order_id}, order_ev)
            else:
                order_ev = cached_order

            ref = order_ev["evidence_ref"]
            evidence_refs.append(ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_agent",
                tool_name="get_order",
                evidence_refs=[ref],
                attributes={"status": order_ev["data"].get("order_status")},
            )
            data = order_ev["data"]
            findings.order_status = data.get("order_status")
            findings.purchase_timestamp = data.get("order_purchase_timestamp")
            findings.approved_at = data.get("order_approved_at")
        except Exception:
            pass

        # 2. get_order_items
        try:
            cached_items = context.get_cached("get_order_items", {"order_id": order_id})
            if cached_items is None:
                items_ev = await self.gateway.call(
                    "get_order_items", case_id=case_id, order_id=order_id
                )
                context.set_cached("get_order_items", {"order_id": order_id}, items_ev)
            else:
                items_ev = cached_items

            ref = items_ev["evidence_ref"]
            evidence_refs.append(ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_agent",
                tool_name="get_order_items",
                evidence_refs=[ref],
                attributes={"item_count": len(items_ev["data"])},
            )
            items_data = items_ev["data"]
            findings.items = items_data
            for item in items_data:
                iid = item.get("order_item_id")
                if iid and str(iid) not in findings.item_ids:
                    findings.item_ids.append(str(iid))

                sid = item.get("seller_id")
                if sid and sid not in findings.seller_ids:
                    findings.seller_ids.append(sid)

                pid = item.get("product_id")
                if pid and pid not in findings.product_ids:
                    findings.product_ids.append(pid)

                try:
                    price = float(item.get("price", 0.0) or 0.0)
                except (ValueError, TypeError):
                    price = 0.0

                try:
                    freight = float(item.get("freight_value", 0.0) or 0.0)
                except (ValueError, TypeError):
                    freight = 0.0

                findings.total_items_price_brl += price
                findings.total_freight_brl += freight

                if sid:
                    if sid not in findings.seller_financials:
                        findings.seller_financials[sid] = {
                            "items": [],
                            "item_price": 0.0,
                            "freight": 0.0,
                            "total": 0.0,
                        }
                    findings.seller_financials[sid]["items"].append(str(iid) if iid else pid)
                    findings.seller_financials[sid]["item_price"] = round(
                        findings.seller_financials[sid]["item_price"] + price, 2
                    )
                    findings.seller_financials[sid]["freight"] = round(
                        findings.seller_financials[sid]["freight"] + freight, 2
                    )
                    findings.seller_financials[sid]["total"] = round(
                        findings.seller_financials[sid]["item_price"]
                        + findings.seller_financials[sid]["freight"],
                        2,
                    )

                deadline = item.get("shipping_limit_date")
                if deadline:
                    findings.shipping_deadlines.append(
                        {
                            "order_item_id": str(iid) if iid else "",
                            "seller_id": sid or "",
                            "shipping_limit_date": str(deadline),
                        }
                    )

            findings.total_items_price_brl = round(findings.total_items_price_brl, 2)
            findings.total_freight_brl = round(findings.total_freight_brl, 2)
            findings.total_order_value_brl = round(
                findings.total_items_price_brl + findings.total_freight_brl, 2
            )
        except Exception:
            pass

        # 3. get_sellers
        try:
            cached_sellers = context.get_cached("get_sellers", {"order_id": order_id})
            if cached_sellers is None:
                sellers_ev = await self.gateway.call(
                    "get_sellers", case_id=case_id, order_id=order_id
                )
                context.set_cached("get_sellers", {"order_id": order_id}, sellers_ev)
            else:
                sellers_ev = cached_sellers

            ref = sellers_ev["evidence_ref"]
            evidence_refs.append(ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_agent",
                tool_name="get_sellers",
                evidence_refs=[ref],
                attributes={"seller_count": len(sellers_ev["data"])},
            )
            sellers_data = sellers_ev["data"]
            for s in sellers_data:
                sid = s.get("seller_id")
                if sid and sid not in findings.seller_ids:
                    findings.seller_ids.append(sid)
                if sid:
                    findings.seller_locations[sid] = {
                        "city": s.get("seller_city", ""),
                        "state": s.get("seller_state", ""),
                        "zip_code_prefix": str(s.get("seller_zip_code_prefix", "")),
                    }
        except Exception:
            pass

        # 4. get_product_context (if required by scope)
        if scope.get("include_product_context", True):
            try:
                cached_prod = context.get_cached("get_product_context", {"order_id": order_id})
                if cached_prod is None:
                    prod_ev = await self.gateway.call(
                        "get_product_context", case_id=case_id, order_id=order_id
                    )
                    context.set_cached("get_product_context", {"order_id": order_id}, prod_ev)
                else:
                    prod_ev = cached_prod

                ref = prod_ev["evidence_ref"]
                evidence_refs.append(ref)
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order_agent",
                    tool_name="get_product_context",
                    evidence_refs=[ref],
                    attributes={"products": len(prod_ev["data"])},
                )
                prod_data = prod_ev["data"]
                for p in prod_data:
                    pid = p.get("product_id")
                    if pid and pid not in findings.product_ids:
                        findings.product_ids.append(pid)
                    cat = p.get("category_name_english") or (p.get("product", {}) or {}).get(
                        "product_category_name"
                    )
                    if cat and cat not in findings.product_categories:
                        findings.product_categories.append(cat)
            except Exception:
                pass

        findings.evidence_refs = evidence_refs
        return findings
