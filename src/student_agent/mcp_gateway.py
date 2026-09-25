from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        # Cache only successful responses, and keep case_id in the cache key so
        # evidence can never leak from one case into another.
        self._case_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Return the server-published tool contracts without guessing tool names."""
        response = await self._session.list_tools()
        tools: list[dict[str, Any]] = []
        for tool in response.tools:
            input_schema = getattr(tool, "inputSchema", None)
            if input_schema is None:
                input_schema = getattr(tool, "input_schema", None)
            tools.append(
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", None),
                    "input_schema": input_schema,
                }
            )
        return sorted(tools, key=lambda item: item["name"])

    async def list_tools(self) -> list[str]:
        """Compatibility helper for callers that need tool names only."""
        return [tool["name"] for tool in await self.discover_tools()]

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        """Call a discovered tool, caching identical successful calls per case."""
        payload = {"case_id": case_id, **arguments}
        cache_key = (
            case_id,
            tool_name,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        cached = self._case_cache.get(cache_key)
        if cached is not None:
            return deepcopy(cached)

        result = await self._session.call_tool(tool_name, arguments=payload)
        is_error = getattr(result, "isError", None)
        if is_error is None:
            is_error = getattr(result, "is_error", False)
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
        self._case_cache[cache_key] = deepcopy(evidence)
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(60.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
