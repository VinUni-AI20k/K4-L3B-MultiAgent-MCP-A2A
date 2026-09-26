from __future__ import annotations

import logging
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger("student_agent.mcp_evidence")


class EvidenceManager:
    """Manages MCP tool invocations, audit tracking, in-case caching, and trace emission."""

    def __init__(
        self,
        case_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        available_tools: list[str] | None = None,
    ) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.available_tools = set(available_tools or [])
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self.all_evidence_refs: list[str] = []
        self.evidence_by_domain: dict[str, list[dict[str, Any]]] = {}

    async def initialize(self) -> None:
        if not self.available_tools:
            try:
                tools = await self.gateway.list_tools()
                self.available_tools = set(tools)
            except Exception as exc:
                logger.warning(f"Could not list tools from gateway: {exc}")
                self.available_tools = set()

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self.available_tools

    def find_matching_tool(self, *candidates: str) -> str | None:
        """Find the first candidate tool that exists in available tools."""
        for c in candidates:
            if c in self.available_tools:
                return c
        return None

    async def call_tool(
        self,
        tool_name: str,
        actor: str,
        **arguments: str,
    ) -> dict[str, Any] | None:
        """Call MCP tool with in-case caching and automatic trace emission."""
        if self.available_tools and tool_name not in self.available_tools:
            logger.info(f"Tool {tool_name} not in available MCP tools: {self.available_tools}")
            return None

        # Sort arguments for deterministic cache key
        cache_key = (tool_name, tuple(sorted((k, str(v)) for k, v in arguments.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            evidence = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
        except Exception as exc:
            logger.warning(f"MCP tool call {tool_name} failed: {exc}")
            return None

        evidence_ref = evidence.get("evidence_ref")
        if evidence_ref:
            if evidence_ref not in self.all_evidence_refs:
                self.all_evidence_refs.append(evidence_ref)
            
            domain = evidence.get("domain", "unknown")
            self.evidence_by_domain.setdefault(domain, []).append(evidence)

            # Emit tool_result_consumed trace event
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
                attributes={"domain": domain},
            )

        self._cache[cache_key] = evidence
        return evidence
