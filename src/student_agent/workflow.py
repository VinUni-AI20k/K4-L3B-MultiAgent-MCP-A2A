from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .conflict_resolver import ConflictResolverAgent
from .coordinator import CoordinatorAgent
from .entity_resolver import EntityResolverAgent
from .mcp_evidence import EvidenceManager
from .mcp_gateway import EvidenceGateway
from .specialists import PaymentAgent, PolicyAgent, ShipmentAgent
from .trace import TraceWriter
from .verifier import VerifierAgent

logger = logging.getLogger("student_agent.workflow")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for Day09 L3B.
    
    Orchestration:
    - Coordinator Agent (Qwen3:8b) for thinking, hypothesis generation, and planning.
    - Specialized Agents (Qwen3:1.7b): EntityResolver, Shipment, Payment, Policy,
      ConflictResolver, and Verifier.
    """
    # Extract normalized case details
    from .cases import extract_case_details

    info = extract_case_details(case)
    case_id = info["case_id"]
    claims = info["claims"]

    # Load model configurations from environment or use defaults
    orchestrator_model = os.getenv("ORCHESTRATOR_MODEL", "qwen3:8b")
    specialist_model = os.getenv("SPECIALIST_MODEL", "qwen3:1.7b")
    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    # 1. MCP Evidence Gateway & Cache initialization (Shared Blackboard)
    evidence_mgr = EvidenceManager(case_id=case_id, gateway=gateway, trace=trace)
    await evidence_mgr.initialize()

    # 2. Instantiate Agents
    coordinator = CoordinatorAgent(model=orchestrator_model, host=ollama_host)
    entity_resolver = EntityResolverAgent(model=specialist_model, host=ollama_host)
    shipment_agent = ShipmentAgent(model=specialist_model, host=ollama_host)
    payment_agent = PaymentAgent(model=specialist_model, host=ollama_host)
    policy_agent = PolicyAgent(model=specialist_model, host=ollama_host)
    conflict_resolver = ConflictResolverAgent(model=specialist_model, host=ollama_host)
    verifier = VerifierAgent(model=specialist_model, host=ollama_host)

    # 3. Coordinator: Deep thinking, context analysis & master plan formulation
    master_plan = await coordinator.analyze_and_plan(case, trace)

    # 4. Phase 1: Entity Resolution & Gate Keeper
    coordinator.assign_entity_task(case_id, trace)
    entity_result = await entity_resolver.resolve(case, evidence_mgr, trace)
    resolved_order_ids = entity_result.get("resolved_order_ids", [])

    # Gate Keeper: Entity Valid?
    if not resolved_order_ids:
        # Fallback Strategy: check candidate_order_ids for any valid hex candidate or claimed_order_id
        claimed = info.get("claimed_order_id")
        valid_cands = [
            str(c).strip() for c in info.get("candidate_order_ids", [])
            if not str(c).startswith("candidate-") and len(str(c)) == 32
        ]
        if claimed and claimed in valid_cands:
            resolved_order_ids = [claimed]
            entity_result["resolved_order_ids"] = resolved_order_ids
            entity_result["status"] = "resolved"
        elif valid_cands:
            resolved_order_ids = [valid_cands[0]]
            entity_result["resolved_order_ids"] = resolved_order_ids
            entity_result["status"] = "resolved"

    # 5. Phase 2: Parallel Specialist Investigation
    coordinator.assign_specialist_tasks(case_id, resolved_order_ids, trace)

    shipment_task = shipment_agent.investigate(case_id, resolved_order_ids, evidence_mgr, trace)
    payment_task = payment_agent.investigate(case_id, resolved_order_ids, evidence_mgr, trace)
    policy_task = policy_agent.investigate(case_id, claims, evidence_mgr, trace)

    shipment_res, payment_res, policy_res = await asyncio.gather(
        shipment_task, payment_task, policy_task
    )

    # 6. Phase 3: Triangulation & Conflict Resolution
    conflict_res = await conflict_resolver.resolve(
        case=case,
        entity_res=entity_result,
        shipment_res=shipment_res,
        payment_res=payment_res,
        policy_res=policy_res,
        trace=trace,
    )

    # 7. Formal Verification & Packaging
    final_output = await verifier.verify_and_package(
        case=case,
        entity_res=entity_result,
        shipment_res=shipment_res,
        payment_res=payment_res,
        policy_res=policy_res,
        conflict_res=conflict_res,
        evidence_refs=evidence_mgr.all_evidence_refs,
        trace=trace,
    )

    return final_output
