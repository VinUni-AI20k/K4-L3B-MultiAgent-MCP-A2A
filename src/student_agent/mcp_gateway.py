from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                is_error = getattr(result, "is_error", getattr(result, "isError", False))
                if is_error:
                    msg_text = " ".join(
                        block.text for block in result.content if getattr(block, "text", None)
                    )
                    err_msg = f"MCP tool {tool_name} failed: {msg_text or 'unknown error'}"
                    if tool_name == "get_refund_timeline":
                        raise RuntimeError(err_msg)
                    last_error = RuntimeError(err_msg)
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    raise last_error
                evidence = getattr(result, "structuredContent", None)
                if evidence is None:
                    evidence = getattr(result, "structured_content", None)
                if evidence is None:
                    text_blocks = [
                        block.text for block in result.content if getattr(block, "text", None)
                    ]
                    if len(text_blocks) != 1:
                        raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
                    evidence = json.loads(text_blocks[0])
                self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
                return evidence
            except Exception as exc:
                if tool_name == "get_refund_timeline":
                    raise
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise last_error from exc
        if last_error:
            raise last_error
        raise RuntimeError(f"MCP tool {tool_name} failed unexpectedly")


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    try:
        from urllib.parse import urlparse
        parsed = urlparse(endpoint)
        base_api = f"{parsed.scheme}://{parsed.netloc}"
        async with httpx2.AsyncClient(headers=headers, timeout=30.0) as init_client:
            await init_client.post(f"{base_api}/api/v2/runs", json={"variant_id": VARIANT_ID})
    except Exception:
        pass

    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
