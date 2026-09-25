from __future__ import annotations

import json
import logging
from typing import Any

from .ollama_client import OllamaAgentClient
from .trace import TraceWriter

logger = logging.getLogger("student_agent.coordinator")

COORDINATOR_SYSTEM_PROMPT = """You are the Lead Coordinator Agent (Orchestrator) for the Brazilian E-Commerce dispute investigation system.
Your job is to analyze incoming cases, think deeply about the facts and potential anomalies, and formulate an actionable investigation plan for specialized sub-agents.

You MUST think before creating the execution plan. Your response MUST follow this exact format:
<think>
1. Case Context & Data Ingestion:
   - Examine case_id, customer claims, hints, and candidate order IDs.
   - Separate potentially valid 32-character hexadecimal order IDs from synthetic dummy IDs (e.g. 'candidate-001').
2. Diagnostic Hypothesis:
   - Identify the likely core issue (e.g. seller dispatch delay vs carrier transit delay, split payment vs overcharge, refund failure, canceled order paid).
3. Delegation Strategy:
   - Entity Resolver Agent: Specify how candidate order IDs and customer unique ID should be vetted.
   - Shipment Agent: Define key timeline milestones (purchase, approval, carrier handoff, customer delivery vs estimated date).
   - Payment Agent: Formulate reconciliation goals (captured vs item + freight total, split payment types, refund audit).
   - Policy Agent: Target relevant EC_POLICY_V2 rules (cancellation, delivery delay compensation, refund rights).
   - Conflict Resolver Agent: Anticipate discrepancies between customer statements and system evidence.
   - Verifier Agent: Outline schema and invariant checks.
</think>

Followed by a JSON object:
{
  "case_id": "<case_id>",
  "initial_hypothesis": "<hypothesis summary>",
  "entity_resolution_plan": {
    "filter_synthetic_candidates": true,
    "target_candidates": ["<id>", ...],
    "customer_hint": "<customer_id or null>"
  },
  "specialist_tasks": {
    "shipment": "Investigate shipping timeline, compare shipping_limit_date vs carrier delivery and estimated delivery date.",
    "payment": "Reconcile payment captured vs order items sum, check for split payments or duplicate charges.",
    "policy": "Evaluate claims against EC_POLICY_V2 compensation and cancellation rules."
  },
  "expected_conflicts": ["delivery_date", "charge_amount"]
}
"""


class CoordinatorAgent:
    """Orchestrator agent powered by Qwen3:8b with explicit thinking and planning."""

    def __init__(
        self,
        model: str = "qwen3:8b",
        host: str | None = None,
    ) -> None:
        self.actor_name = "coordinator"
        self.client = OllamaAgentClient(
            model=model,
            system_prompt=COORDINATOR_SYSTEM_PROMPT,
            host=host,
            temperature=0.1,
        )

    async def analyze_and_plan(
        self,
        case: dict[str, Any],
        trace: TraceWriter,
    ) -> dict[str, Any]:
        """Analyze case input, produce reasoning (<think>), and generate the master plan."""
        case_id = case.get("case_id", "UNKNOWN")
        claims = case.get("claims", [])
        candidates = case.get("candidate_order_ids", [])
        hints = case.get("hints", {})
        customer_id = case.get("customer_unique_id") or hints.get("customer_unique_id")

        prompt = (
            f"Case ID: {case_id}\n"
            f"Claims: {json.dumps(claims, ensure_ascii=False)}\n"
            f"Candidate Order IDs: {json.dumps(candidates, ensure_ascii=False)}\n"
            f"Customer Identifier/Hints: {json.dumps(hints, ensure_ascii=False)}\n"
            f"Explicit Customer Unique ID: {customer_id}\n\n"
            "Analyze this case thoroughly, structure your thinking in <think> tags, and output the plan JSON."
        )

        thinking, content, plan_json = await self.client.chat(prompt)

        # Fallback plan if LLM output could not be parsed
        if not plan_json:
            plan_json = {
                "case_id": case_id,
                "initial_hypothesis": "Investigate candidate orders, shipping timeline, and payment records.",
                "entity_resolution_plan": {
                    "filter_synthetic_candidates": True,
                    "target_candidates": [c for c in candidates if not str(c).startswith("candidate-")],
                    "customer_hint": customer_id,
                },
                "specialist_tasks": {
                    "shipment": "Examine order delivery timeline and dispatch delay vs carrier transit.",
                    "payment": "Reconcile captured total vs item value and payment vouchers/cards.",
                    "policy": "Check EC_POLICY_V2 policy rules on return and compensation.",
                },
                "expected_conflicts": [],
            }

        logger.info(f"Coordinator plan generated for {case_id}. Thinking length: {len(thinking)}")
        return plan_json

    def assign_entity_task(self, case_id: str, trace: TraceWriter) -> None:
        """Emit task_assigned event to Entity Resolver Agent."""
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor=self.actor_name,
            target="entity_resolver",
            attributes={"task": "resolve_order_and_customer_identity"},
        )

    def assign_specialist_tasks(
        self,
        case_id: str,
        resolved_order_ids: list[str],
        trace: TraceWriter,
    ) -> None:
        """Emit task_assigned events to Shipment, Payment, and Policy specialists."""
        for target in ["shipment_agent", "payment_agent", "policy_agent"]:
            trace.emit(
                case_id=case_id,
                event_type="task_assigned",
                actor=self.actor_name,
                target=target,
                attributes={
                    "task": f"investigate_{target.split('_')[0]}",
                    "target_orders": resolved_order_ids[0] if resolved_order_ids else None,
                },
            )
