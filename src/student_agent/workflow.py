"""Day09 L3B Multi-Agent Workflow Entrypoint."""

from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .pipeline.coordinator import CoordinatorAgent
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the Day09 L3B Multi-Agent Google ADK pipeline.

    Orchestrates:
    1. Coordinator: Entity resolution and specialist routing.
    2. Order Agent: Product catalog, items, sellers, and financial reconciliation.
    3. Shipment Agent: Delivery timelines, milestones, deadlines, and delay attribution.
    4. Payment Agent: Transaction reconciliation, refunds, and duplicate charges.
    5. Policy Agent: Dispute adjudication and conflict resolution.
    6. Verifier Agent: Invariants, schema validation, and evidence provenance audit.
    """
    coordinator = CoordinatorAgent(gateway, trace, trace.contracts)
    return await coordinator.solve(case)
