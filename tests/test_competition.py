import asyncio
from pathlib import Path

import pytest

from student_agent.competition import start_run
from student_agent.config import Settings


@pytest.mark.parametrize("status", [200, 401, 403, 409, 500])
def test_run_lifecycle_uses_team_auth_without_redirects(monkeypatch, status):
    settings = Settings("https://competition.example", "test-secret", "https://mcp.example", Path())

    class Response:
        status_code = status

        def json(self):
            return {"id": "run-test", "variant_id": "l3b"}

    class Client:
        def __init__(self, *, timeout, follow_redirects):
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, headers, json):
            assert url == "https://competition.example/api/v2/runs"
            assert headers == {"Authorization": "Bearer test-secret"}
            assert json == {"variant_id": "l3b"}
            return Response()

    monkeypatch.setattr("student_agent.competition.httpx2.AsyncClient", Client)
    if status == 200:
        assert asyncio.run(start_run(settings))["id"] == "run-test"
    else:
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            asyncio.run(start_run(settings))
