from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.model import JsonModel


class Session:
    def __init__(self):
        self.cursors = []

    async def list_tools(self, *, params=None):
        cursor = params.cursor if params else None
        self.cursors.append(cursor)
        tool = Tool(
            name="get_order" if cursor is None else "get_policy",
            inputSchema={"type": "object"},
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        )
        return ListToolsResult(tools=[tool], nextCursor="page2" if cursor is None else None)

    async def call_tool(self, name, arguments):
        return CallToolResult(
            structuredContent={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_" + "a" * 24,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": arguments,
            },
            content=[],
        )


def test_gateway_uses_installed_v2_sdk_and_caches_discovery():
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    session = Session()
    gateway = EvidenceGateway(session, contracts)
    assert asyncio.run(gateway.list_tools()) == ["get_order", "get_policy"]
    assert asyncio.run(gateway.list_tools()) == ["get_order", "get_policy"]
    assert session.cursors == [None, "page2"]
    tools = asyncio.run(gateway.discover_tools())
    assert tools[0]["readOnly"] is True
    assert tools[0]["destructive"] is False
    result = asyncio.run(gateway.call("get_order", case_id="CASE_001"))
    assert result["data"]["case_id"] == "CASE_001"


def test_gateway_rejects_mcp_error():
    class ErrorSession(Session):
        async def call_tool(self, name, arguments):
            return CallToolResult(isError=True, content=[TextContent(type="text", text="failed")])

    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    with pytest.raises(RuntimeError, match="MCP tool"):
        asyncio.run(
            EvidenceGateway(ErrorSession(), contracts).call("get_order", case_id="CASE_001")
        )


def test_ollama_json_protocol(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL_PARAMETERS_B", "8")
    monkeypatch.setenv("LLM_API_KEY", "do-not-send-to-local-ollama")

    def respond(request, timeout):
        assert request.full_url == "http://localhost:11434/api/chat"
        assert "Authorization" not in request.headers
        body = json.loads(request.data)
        assert body["think"] is False
        assert body["stream"] is False
        assert body["model"] == "qwen3:8b"
        return io.BytesIO(json.dumps({"message": {"content": '{"ok":true}'}}).encode())

    monkeypatch.setattr("student_agent.model.urlopen", respond)
    assert asyncio.run(JsonModel().complete("JSON only", {})) == {"ok": True}


@pytest.mark.parametrize("size", ["0", "11", "nan", "inf"])
def test_model_parameter_limit(monkeypatch, size):
    monkeypatch.setenv("LLM_MODEL_PARAMETERS_B", size)
    with pytest.raises(ValueError, match="PARAMETERS"):
        JsonModel()
