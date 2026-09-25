from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import httpx2
from jsonschema import Draft202012Validator

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PERMISSIONS = {
    "entity-agent": {"get_customer_history", "get_order"},
    "order-agent": {"get_order", "get_order_items", "get_product_context", "get_sellers"},
    "shipment-agent": {"get_shipment_summary", "get_order_items"},
    "payment-agent": {"get_order_payments", "get_payment_timeline", "get_refund_timeline"},
    "policy-agent": {"get_policy"},
}


class EvidenceCollector:
    """One instance per case/run. All allowed tools are read-only evidence tools."""

    def __init__(
        self,
        case_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        tools: dict[str, dict[str, Any]],
        *,
        budget: int = 20,
        timeout: float = 30,
        backoff: float = 1,
    ) -> None:
        self.case_id, self.gateway, self.trace = case_id, gateway, trace
        self.tools = deepcopy(tools)
        self.budget, self.timeout, self.backoff = budget, timeout, backoff
        self.calls = 0
        self.registry: dict[str, dict[str, Any]] = {}
        self.consumed: dict[str, set[str]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            if self.calls >= self.budget:
                raise RuntimeError("MCP_BUDGET_EXHAUSTED")
            # No await between check and increment: atomic within this event loop.
            self.calls += 1
            try:
                async with asyncio.timeout(self.timeout):
                    evidence = await self.gateway.call(tool, case_id=self.case_id, **arguments)
                self.trace.contracts.validate_evidence(evidence)
                ref = evidence["evidence_ref"]
                record = {
                    "case_id": self.case_id,
                    "tool": tool,
                    "arguments": deepcopy(arguments),
                    "evidence": deepcopy(evidence),
                }
                if ref in self.registry and self.registry[ref] != record:
                    raise ValueError("MCP evidence ref reused for different content/request")
                self.registry[ref] = record
                return evidence
            except (TimeoutError, httpx2.TransportError):
                if attempt:
                    raise
                await asyncio.sleep(self.backoff)
        raise AssertionError("unreachable")

    async def call(self, actor: str, tool: str, **arguments: Any) -> dict[str, Any]:
        if tool not in PERMISSIONS.get(actor, set()):
            raise ValueError(f"Tool permission denied: {actor}/{tool}")
        if tool not in self.tools:
            raise ValueError(f"Tool not discovered: {tool}")
        if "case_id" in arguments:
            raise ValueError("case_id is owned by the collector")
        payload = {"case_id": self.case_id, **arguments}
        Draft202012Validator(self.tools[tool]).validate(payload)
        key = json.dumps([tool, payload], sort_keys=True, allow_nan=False)
        if key not in self._tasks:
            self._tasks[key] = asyncio.create_task(self._fetch(tool, arguments))
        task = self._tasks[key]
        try:
            return deepcopy(await asyncio.shield(task))
        finally:
            if task.done() and (task.cancelled() or task.exception() is not None):
                self._tasks.pop(key, None)

    def consume(self, actor: str, refs: list[str]) -> None:
        # Call only when the agent actually uses the returned data.
        for ref in dict.fromkeys(refs):
            record = self.registry.get(ref)
            if record is None or record["tool"] not in PERMISSIONS.get(actor, set()):
                raise ValueError("Unknown or unauthorized evidence consumption")
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=record["tool"],
                evidence_refs=[ref],
            )
            self.consumed.setdefault(actor, set()).add(ref)
