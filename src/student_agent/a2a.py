from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from .evidence_collector import EvidenceCollector


@dataclass(frozen=True)
class Task:
    case_id: str
    recipient: str
    task_type: str
    payload: dict[str, Any]
    entity_scope: tuple[str, ...] = ()
    sender: str = "coordinator"
    message_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class Result:
    case_id: str
    sender: str
    message_id: str  # Echo the assigned task ID for correlation.
    payload: dict[str, Any]
    evidence_refs: tuple[str, ...] = ()
    status: str = "completed"
    error_code: str | None = None


class Specialist(Protocol):
    async def __call__(self, task: Task, collector: EvidenceCollector) -> Result: ...


def validate_result(task: Task, result: Result, collector: EvidenceCollector) -> None:
    if (result.case_id, result.sender, result.message_id) != (
        task.case_id,
        task.recipient,
        task.message_id,
    ):
        raise ValueError("A2A correlation mismatch")
    if result.status != "completed" or result.error_code is not None:
        raise RuntimeError(f"Specialist failed: {result.sender}")
    if not isinstance(result.payload, dict):
        raise ValueError("Specialist payload must be an object")
    if not set(result.evidence_refs) <= collector.consumed.get(result.sender, set()):
        raise ValueError("Specialist returned unconsumed/foreign evidence")
