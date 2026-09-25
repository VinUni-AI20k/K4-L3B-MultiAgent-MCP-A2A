"""Shared, append-only state used by the complaint-investigation workflow."""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict


class ComplaintState(TypedDict):
    """The supervisor may route work, while workers only return state patches.

    ``evidence`` and ``errors`` use reducers so a later worker cannot discard an
    earlier worker's audit information.
    """

    case_id: str
    case: dict[str, Any]
    available_tools: tuple[str, ...]
    current_worker: str
    iteration_count: int
    evidence: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    entity_confidence: float
    analysis: dict[str, Any]
    final_output: NotRequired[dict[str, Any]]
