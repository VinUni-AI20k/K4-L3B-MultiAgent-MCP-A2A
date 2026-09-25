from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from student_agent.bedrock_model import BedrockIssueModel


def test_bedrock_uses_eight_billion_model_and_converse_api() -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "output": {
                    "message": {
                        "content": [{"text": '{"primary_issue":"duplicate_charge"}'}]
                    }
                }
            },
        )

    model = BedrockIssueModel(
        api_key="test-key", region="us-east-1", transport=httpx2.MockTransport(respond)
    )
    result = asyncio.run(
        model.select_issue(
            {"case_id": "CASE_PRIVATE", "customer_request": {"claims": []}},
            ["duplicate_charge", "late_delivery_logistics"],
            [
                {
                    "evidence_ref": "REF_PRIVATE",
                    "domain": "payment",
                    "data": {"issue_code": "duplicate_charge", "order_id": "ORDER_PRIVATE"},
                }
            ],
        )
    )
    request = requests[0]
    payload = json.loads(request.content)
    assert result == "duplicate_charge"
    assert request.url == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
        "us.meta.llama3-1-8b-instruct-v1:0/converse"
    )
    assert request.headers["Authorization"] == "Bearer test-key"
    prompt = payload["messages"][0]["content"][0]["text"]
    assert "duplicate_charge" in prompt
    assert not any(secret in prompt for secret in ("CASE_PRIVATE", "REF_PRIVATE", "ORDER_PRIVATE"))


def test_bedrock_rejects_issue_outside_candidates() -> None:
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(
            200,
            json={
                "output": {
                    "message": {"content": [{"text": '{"primary_issue":"refund_failed"}'}]}
                }
            },
        )
    )
    model = BedrockIssueModel(
        api_key="test-key", region="us-east-1", transport=transport
    )
    with pytest.raises(ValueError, match="candidate"):
        asyncio.run(model.select_issue({}, ["duplicate_charge"], []))


def test_bedrock_requires_token_and_region() -> None:
    with pytest.raises(ValueError, match="AWS_BEARER_TOKEN_BEDROCK"):
        BedrockIssueModel(api_key="", region="us-east-1")
    with pytest.raises(ValueError, match="AWS_DEFAULT_REGION"):
        BedrockIssueModel(api_key="token", region="")
