from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class GatewayUnavailable(RuntimeError):
    """The gateway keeps failing every call; stop the run instead of spending more calls."""


class EvidenceGateway:
    def __init__(
        self, session: ClientSession, contracts: Contracts, max_consecutive_failures: int = 5
    ) -> None:
        self._session = session
        self._contracts = contracts
        self._max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures = 0

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        # Every call is audited and counts against efficiency, so a gateway that rejects
        # everything must not be hammered for the rest of the batch.
        if self._consecutive_failures >= self._max_consecutive_failures:
            raise GatewayUnavailable(
                f"{self._consecutive_failures} consecutive MCP calls failed; run aborted"
            )
        try:
            evidence = await self._call_once(tool_name, case_id=case_id, **arguments)
        except Exception:
            self._consecutive_failures += 1
            raise
        self._consecutive_failures = 0
        return evidence

    async def _call_once(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        # mcp v2 renamed isError -> is_error; accept both SDK generations.
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    # No transport read timeout: a ReadTimeout raised in the transport task group tears down
    # the whole session. Per-call deadlines and retries are enforced by CaseContext.fetch.
    timeout = httpx2.Timeout(None, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
