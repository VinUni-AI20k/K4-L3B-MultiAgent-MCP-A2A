from __future__ import annotations

import json
import logging
import re
from typing import Any

from .mcp_evidence import EvidenceManager
from .ollama_client import OllamaAgentClient
from .trace import TraceWriter

logger = logging.getLogger("student_agent.entity_resolver")

HEX_UUID_32_REGEX = re.compile(r"^[0-9a-fA-F]{32}$")

ENTITY_RESOLVER_SYSTEM_PROMPT = """You are the Entity Resolver Agent for Brazilian E-Commerce dispute cases.
Your responsibility:
1. Examine candidate order IDs. Any candidate formatted like 'candidate-xxx', or non-hexadecimal dummy values MUST be classified as rejected candidates.
2. Cross-reference customer history, customer_unique_id, and order details retrieved via MCP.
3. Determine the definitive resolved order ID(s) and customer unique ID.

Output valid JSON:
{
  "status": "resolved" | "ambiguous" | "not_found",
  "resolved_order_ids": ["<order_id>"],
  "rejected_candidates": ["<rejected_candidate_id>", ...],
  "customer_unique_id": "<customer_unique_id>",
  "related_order_ids": ["<order_id>", ...],
  "confidence": 0.95
}
"""


class EntityResolverAgent:
    """Specialist agent powered by Qwen3:1.7b resolving orders and customer identities."""

    def __init__(
        self,
        model: str = "qwen3:1.7b",
        host: str | None = None,
    ) -> None:
        self.actor_name = "entity_resolver"
        self.client = OllamaAgentClient(
            model=model,
            system_prompt=ENTITY_RESOLVER_SYSTEM_PROMPT,
            host=host,
            temperature=0.0,
        )

    async def resolve(
        self,
        case: dict[str, Any],
        evidence_mgr: EvidenceManager,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        from .cases import extract_case_details

        info = extract_case_details(case)
        case_id = info["case_id"]
        candidate_order_ids = info["candidate_order_ids"]
        customer_unique_id = info["customer_unique_id"]
        claimed_order_id = info["claimed_order_id"]

        # 1. Immediately identify synthetic garbage IDs (candidate-xxx)
        rejected_candidates: list[str] = []
        valid_candidates: list[str] = []
        for cand in candidate_order_ids:
            cand_str = str(cand).strip()
            if cand_str.startswith("candidate-") or not HEX_UUID_32_REGEX.match(cand_str):
                rejected_candidates.append(cand_str)
            else:
                valid_candidates.append(cand_str)

        # 2. Query MCP evidence: customer history / orders
        customer_data = None
        customer_history_tool = evidence_mgr.find_matching_tool(
            "get_customer_history", "lookup_customer", "get_customer"
        )
        if customer_history_tool and customer_unique_id:
            res = await evidence_mgr.call_tool(
                customer_history_tool,
                actor=self.actor_name,
                customer_unique_id=customer_unique_id,
            )
            if res:
                customer_data = res.get("data")

        # Also lookup candidates directly if needed
        order_tool = evidence_mgr.find_matching_tool(
            "get_order", "get_order_details", "lookup_order"
        )
        known_order_ids: list[str] = []
        if customer_data and isinstance(customer_data, dict):
            # Extract customer unique ID if not known
            c_uid = customer_data.get("customer_unique_id") or customer_data.get("customer_id")
            if c_uid:
                customer_unique_id = c_uid
            orders = customer_data.get("orders") or customer_data.get("order_ids") or []
            if isinstance(orders, list):
                for o in orders:
                    o_id = o.get("order_id") if isinstance(o, dict) else str(o)
                    if o_id and o_id not in known_order_ids:
                        known_order_ids.append(o_id)

        # Match valid candidates against known orders or check them with order_tool
        resolved_order_ids: list[str] = []
        for cand in valid_candidates:
            if known_order_ids and cand in known_order_ids:
                resolved_order_ids.append(cand)
            elif order_tool:
                # Check with order tool
                order_res = await evidence_mgr.call_tool(
                    order_tool,
                    actor=self.actor_name,
                    order_id=cand,
                )
                if order_res and order_res.get("data"):
                    resolved_order_ids.append(cand)
                else:
                    rejected_candidates.append(cand)
            else:
                # Default to keeping valid hex candidate if no conflicting evidence
                resolved_order_ids.append(cand)

        # 3. Call Qwen 1.7b to confirm classification
        prompt = (
            f"Case ID: {case_id}\n"
            f"Candidate IDs from Case: {json.dumps(candidate_order_ids)}\n"
            f"Pre-filtered Hex Candidates: {json.dumps(valid_candidates)}\n"
            f"Pre-filtered Rejected Candidates: {json.dumps(rejected_candidates)}\n"
            f"Customer Unique ID: {customer_unique_id}\n"
            f"Known Customer Orders from MCP: {json.dumps(known_order_ids)}\n"
            "Return the entity resolution result in strict JSON format."
        )

        _, _, parsed = await self.client.chat(prompt)

        status = "resolved" if resolved_order_ids else ("not_found" if not candidate_order_ids else "ambiguous")
        confidence = 0.95 if status == "resolved" else (0.5 if status == "ambiguous" else 0.1)

        if parsed:
            # Validate and merge LLM resolution with verified MCP facts
            llm_resolved = [
                oid for oid in parsed.get("resolved_order_ids", [])
                if oid in valid_candidates and not oid.startswith("candidate-")
            ]
            if llm_resolved:
                resolved_order_ids = llm_resolved
            status = parsed.get("status", status)
            if status not in ["resolved", "ambiguous", "not_found"]:
                status = "resolved" if resolved_order_ids else "not_found"
            confidence = float(parsed.get("confidence", confidence))
            confidence = max(0.0, min(1.0, confidence))

        # Ensure no overlap between resolved and rejected
        final_rejected = [c for c in candidate_order_ids if c not in resolved_order_ids]
        related_orders = list(set(known_order_ids + resolved_order_ids))

        result = {
            "status": status,
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": final_rejected,
            "customer_unique_id": customer_unique_id,
            "related_order_ids": related_orders,
            "confidence": confidence,
        }

        # Emit handoff event back to coordinator
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor_name,
            target="coordinator",
            attributes={
                "status": status,
                "resolved_count": len(resolved_order_ids),
                "rejected_count": len(final_rejected),
            },
        )

        return result
