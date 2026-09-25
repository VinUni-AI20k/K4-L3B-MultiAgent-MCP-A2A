from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx2
from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway, GatewayUnavailable
from .trace import TraceWriter

COORDINATOR = "coordinator"
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, httpx2.TransportError)
TRACE_MAX_REFS = 20


class ToolPermissionError(PermissionError):
    """An actor asked for a tool outside its least-privilege allowlist."""


class ProtocolViolation(RuntimeError):
    """An A2A message broke case scope, addressing or the hop limit."""


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool_name: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolFailure:
    actor: str
    tool_name: str
    code: str
    detail: str


@dataclass(frozen=True)
class A2AMessage:
    """Envelope exchanged between agents. Correlated by case_id and never crosses cases."""

    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()


def unique_refs(*groups: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for group in groups for ref in group))


class Agent:
    actor: ClassVar[str]
    tools: ClassVar[frozenset[str]] = frozenset()

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        raise NotImplementedError(f"{self.actor} has no handler yet")

    def reply(
        self,
        message: A2AMessage,
        intent: str,
        payload: dict[str, Any],
        evidence_refs: Iterable[str] = (),
        recipient: str | None = None,
    ) -> A2AMessage:
        return A2AMessage(
            case_id=message.case_id,
            sender=self.actor,
            recipient=recipient or message.sender,
            intent=intent,
            payload=payload,
            evidence_refs=unique_refs(evidence_refs),
        )


class CaseContext:
    """Everything one case may touch: scoped MCP access, evidence ledger and trace.

    A new context is created per case, so cache and evidence can never leak across cases.
    """

    def __init__(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
        *,
        call_budget: int = 12,
        max_attempts: int = 2,
        call_timeout: float = 60.0,
        max_hops: int = 8,
    ) -> None:
        self.case = case
        self.case_id: str = case["case_id"]
        self.call_budget = call_budget
        self.max_attempts = max_attempts
        self.call_timeout = call_timeout
        self.max_hops = max_hops
        self.calls_made = 0
        self.evidence: dict[str, Evidence] = {}
        self.failures: list[ToolFailure] = []
        self.contracts = trace.contracts
        self._gateway = gateway
        self._trace = trace
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], asyncio.Task[Any]] = {}
        self._consumed: set[tuple[str, str]] = set()

    def consumed_refs(self) -> set[str]:
        return {ref for _, ref in self._consumed}

    async def fetch(self, agent: Agent, tool_name: str, **arguments: str) -> Evidence | None:
        """Call one MCP tool for this case; returns None when evidence is unavailable."""
        if tool_name not in agent.tools:
            raise ToolPermissionError(f"{agent.actor} may not call {tool_name}")
        key = (tool_name, tuple(sorted(arguments.items())))
        task = self._cache.get(key)
        if task is None:
            task = asyncio.ensure_future(self._call(agent.actor, tool_name, arguments))
            self._cache[key] = task
        evidence: Evidence | None = await task
        if evidence is not None and (agent.actor, evidence.ref) not in self._consumed:
            self._consumed.add((agent.actor, evidence.ref))
            self.emit(
                "tool_result_consumed",
                actor=agent.actor,
                tool_name=tool_name,
                evidence_refs=[evidence.ref],
                attributes={"domain": evidence.domain, "warnings": len(evidence.warnings)},
            )
        return evidence

    async def _call(self, actor: str, tool_name: str, arguments: dict[str, str]) -> Evidence | None:
        last_error = "no attempt made"
        for _ in range(self.max_attempts):
            if self.calls_made >= self.call_budget:
                return self._fail(actor, tool_name, "budget_exhausted", f"{self.call_budget} calls")
            self.calls_made += 1
            try:
                async with asyncio.timeout(self.call_timeout):
                    envelope = await self._gateway.call(
                        tool_name, case_id=self.case_id, **arguments
                    )
            except TRANSIENT_ERRORS as exc:
                last_error = type(exc).__name__
                continue
            except GatewayUnavailable:
                raise
            except (RuntimeError, MCPError) as exc:
                return self._fail(actor, tool_name, "tool_error", str(exc))
            except ValueError as exc:
                return self._fail(actor, tool_name, "invalid_response", str(exc))
            evidence = Evidence(
                ref=envelope["evidence_ref"],
                tool_name=tool_name,
                domain=envelope["domain"],
                data=envelope["data"],
                warnings=tuple(envelope.get("warnings", ())),
            )
            self.evidence[evidence.ref] = evidence
            return evidence
        return self._fail(actor, tool_name, "transient_exhausted", last_error)

    def _fail(self, actor: str, tool_name: str, code: str, detail: str) -> None:
        self.failures.append(ToolFailure(actor, tool_name, code, detail[:160]))
        return None

    async def dispatch(self, agent: Agent, message: A2AMessage) -> A2AMessage:
        """Deliver one message to its recipient and trace the resulting handoff."""
        if message.case_id != self.case_id:
            raise ProtocolViolation(f"message for {message.case_id} delivered in {self.case_id}")
        if message.recipient != agent.actor:
            raise ProtocolViolation(f"message for {message.recipient} delivered to {agent.actor}")
        if message.sender == COORDINATOR:
            self.emit(
                "task_assigned",
                actor=COORDINATOR,
                target=agent.actor,
                decision_code=message.intent,
            )
        result = await agent.handle(self, message)
        if result.case_id != self.case_id or result.sender != agent.actor:
            raise ProtocolViolation(f"{agent.actor} returned a message outside its scope")
        unknown = [ref for ref in result.evidence_refs if ref not in self.evidence]
        if unknown:
            raise ProtocolViolation(f"{agent.actor} cited evidence it never received: {unknown}")
        self.emit(
            "handoff",
            actor=result.sender,
            target=result.recipient,
            decision_code=result.intent,
            evidence_refs=list(result.evidence_refs[:TRACE_MAX_REFS]) or None,
        )
        return result

    async def route(self, agents: dict[str, Agent], message: A2AMessage) -> A2AMessage:
        """Follow a handoff chain until it returns to the coordinator."""
        for _ in range(self.max_hops):
            agent = agents.get(message.recipient)
            if agent is None:
                raise ProtocolViolation(f"unknown recipient {message.recipient}")
            message = await self.dispatch(agent, message)
            if message.recipient == COORDINATOR:
                return message
        raise ProtocolViolation(f"handoff chain exceeded {self.max_hops} hops")

    def emit(self, event_type: str, *, actor: str, **fields: Any) -> None:
        self._trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **fields)
