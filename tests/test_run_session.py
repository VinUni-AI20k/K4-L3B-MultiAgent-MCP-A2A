import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest

from student_agent import cli


@pytest.mark.parametrize("version", ["test-v1", "wrong-v1"])
def test_run_is_initialized_with_team_auth_and_version_checked(monkeypatch, version):
    settings = SimpleNamespace(
        competition_api_url="https://competition.test",
        team_api_key="test-key",
        mcp_endpoint="https://gateway.test/mcp",
    )
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v2/runs"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {"variant_id": "l3b"}
        return httpx2.Response(
            201,
            json={
                "case_set_version": version,
                "mcp_endpoint": settings.mcp_endpoint,
                "expires_at": "2026-09-25T14:00:00Z",
            },
        )

    client_class = httpx2.AsyncClient
    monkeypatch.setattr(
        cli.httpx2,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx2.MockTransport(handle), **kwargs),
    )
    if version == "test-v1":
        asyncio.run(cli._ensure_run(settings, "test-v1"))
    else:
        with pytest.raises(ValueError, match="Local case-set differs"):
            asyncio.run(cli._ensure_run(settings, "test-v1"))
    assert len(requests) == 1
