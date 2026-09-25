"""Day09 L3B Multi-Agent Workflow Entrypoint."""

from __future__ import annotations

import os
from typing import Any

from .agents import (
    EntityAgent,
    InvestigationContext,
    OrderProductAgent,
    PaymentRefundAgent,
    PolicyConflictAgent,
    ShipmentAgent,
    VerifierAgent,
)
from .mcp_gateway import EvidenceGateway
from .pipeline.coordinator import CoordinatorAgent
from .trace import TraceWriter


async def solve_case_pipeline(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    coordinator = CoordinatorAgent(gateway, trace, trace.contracts)
    return await coordinator.solve(case)


async def solve_case_direct(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    ctx = InvestigationContext(case=case, case_id=case_id, gateway=gateway, trace=trace)

    entity_agent = EntityAgent(ctx)
    entity_output = await entity_agent.run()
    resolved_order_ids = entity_output["entity_resolution"]["resolved_order_ids"]

    order_agent = OrderProductAgent(ctx)
    order_output = await order_agent.run(resolved_order_ids)

    shipment_agent = ShipmentAgent(ctx)
    shipment_output = await shipment_agent.run(resolved_order_ids)

    payment_agent = PaymentRefundAgent(ctx)
    payment_output = await payment_agent.run(resolved_order_ids)

    policy_agent = PolicyConflictAgent(ctx)
    policy_output = await policy_agent.run(
        entity_res=entity_output["entity_resolution"],
        shipment_res=shipment_output,
        payment_res=payment_output,
        seller_ids=order_output["seller_ids"],
        order_res=order_output,
    )

    # 6. Verifier Agent (Cross-field Consistency & Confidence Calibration)
    verifier_agent = VerifierAgent(ctx)
    verified_policy = verifier_agent.verify_and_finalize(
        entity_output=entity_output,
        order_output=order_output,
        shipment_output=shipment_output,
        payment_output=payment_output,
        policy_output=policy_output,
    )

    final_output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": verified_policy["assessment"],
        "affected_entities": {
            "order_ids": resolved_order_ids,
            "item_ids": order_output["item_ids"],
            "seller_ids": order_output["seller_ids"],
            "payment_references": payment_output["payment_references"],
            "shipment_ids": shipment_output["shipment_ids"],
        },
        "claim_assessments": verified_policy.get("claim_assessments", []),
        "entity_resolution": entity_output["entity_resolution"],
        "customer_context": entity_output["customer_context"],
        "shipment_analysis": shipment_output["shipment_analysis"],
        "payment_analysis": payment_output["payment_analysis"],
        "root_cause_analysis": verified_policy["root_cause_analysis"],
        "evidence_refs": ctx.all_evidence_refs[:30],
        "data_conflicts": verified_policy["data_conflicts"],
        "financial_resolution": verified_policy["financial_resolution"],
        "resolution_actions": verified_policy["resolution_actions"],
    }
    return final_output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the Day09 L3B Multi-Agent pipeline."""
    if os.getenv("USE_DIRECT_AGENTS", "0") == "1":
        return await solve_case_direct(case, gateway, trace)
    coordinator = CoordinatorAgent(gateway, trace, trace.contracts)
    return await coordinator.solve(case)
