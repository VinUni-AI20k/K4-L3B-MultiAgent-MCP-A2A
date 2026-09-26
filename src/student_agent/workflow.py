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
    case_id = case.get("case_id", "UNKNOWN")

    # Load model configurations from environment or use defaults
    orchestrator_model = os.getenv("ORCHESTRATOR_MODEL", "qwen3:8b")
    specialist_model = os.getenv("SPECIALIST_MODEL", "qwen3:1.7b")
    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    # 1. MCP Evidence Gateway & Cache initialization
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

    # 4. Coordinator assigns task to Entity Resolver
    coordinator.assign_entity_task(case_id, trace)

    # 5. Entity Resolver: Vets candidates, queries MCP, filters out synthetic candidate-xxx
    entity_result = await entity_resolver.resolve(case, evidence_mgr, trace)
    resolved_order_ids = entity_result.get("resolved_order_ids", [])

    # 6. Coordinator assigns tasks to Specialists
    coordinator.assign_specialist_tasks(case_id, resolved_order_ids, trace)

    # 7. Specialists execute domain investigations concurrently
    shipment_task = shipment_agent.investigate(case_id, resolved_order_ids, evidence_mgr, trace)
    payment_task = payment_agent.investigate(case_id, resolved_order_ids, evidence_mgr, trace)
    policy_task = policy_agent.investigate(case_id, case.get("claims", []), evidence_mgr, trace)

    shipment_res, payment_res, policy_res = await asyncio.gather(
        shipment_task, payment_task, policy_task
    )

    # 8. Conflict Resolver: Applies source precedence, reconciles claims vs logs, synthesizes root causes
    conflict_res = await conflict_resolver.resolve(
        case=case,
        entity_res=entity_result,
        shipment_res=shipment_res,
        payment_res=payment_res,
        policy_res=policy_res,
        trace=trace,
    )

    # 9. Verifier: Validates invariants, checks schema consistency, calibrates confidence, packages output
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
