from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from student_agent.groq_model import GroqIssueModel


def test_groq_uses_allam_7b_and_json_mode() -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": '{"primary_issue":"duplicate_charge"}'}}]},
        )

    model = GroqIssueModel(api_key="test-key", transport=httpx2.MockTransport(respond))
    result = asyncio.run(
        model.select_issue(
            {"case_id": "L3B_CASE_001", "customer_request": {"claims": []}},
            ["duplicate_charge", "late_delivery_logistics"],
            [{"evidence_ref": "ev_" + "a" * 22, "domain": "payment", "data": {}}],
        )
    )
    assert result == "duplicate_charge"
    request = requests[0]
    body = json.loads(request.content)
    assert request.url == "https://api.groq.com/openai/v1/chat/completions"
    assert body["model"] == "allam-2-7b"
    assert body["response_format"] == {"type": "json_object"}
    assert request.headers["Authorization"] == "Bearer test-key"


def test_groq_requires_key() -> None:
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        GroqIssueModel(api_key="")


def test_groq_payload_excludes_case_ids_refs_and_raw_evidence() -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": '{"primary_issue":"duplicate_charge"}'}}]},
        )

    model = GroqIssueModel(api_key="test-key", transport=httpx2.MockTransport(respond))
    asyncio.run(
        model.select_issue(
            {
                "case_id": "PRIVATE_CASE_ID",
                "customer_request": {
                    "claims": [{"topic": "duplicate_charge", "text": "PRIVATE_TEXT"}]
                },
            },
            ["duplicate_charge", "late_delivery_logistics"],
            [
                {
                    "evidence_ref": "PRIVATE_EVIDENCE_REF",
                    "domain": "payment",
                    "data": {
                        "issue_code": "duplicate_charge",
                        "customer_name": "PRIVATE_CUSTOMER_NAME",
                        "order_id": "PRIVATE_ORDER_ID",
                    },
                }
            ],
        )
    )
    body = json.loads(requests[0].content)
    prompt = body["messages"][1]["content"]
    assert "duplicate_charge" in prompt
    assert not any(
        secret in prompt
        for secret in (
            "PRIVATE_CASE_ID",
            "PRIVATE_TEXT",
            "PRIVATE_EVIDENCE_REF",
            "PRIVATE_CUSTOMER_NAME",
            "PRIVATE_ORDER_ID",
        )
    )


def test_groq_rejects_issue_outside_evidence_candidates() -> None:
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(
            200, json={"choices": [{"message": {"content": '{"primary_issue":"refund_failed"}'}}]}
        )
    )
    model = GroqIssueModel(api_key="test-key", transport=transport)
    with pytest.raises(ValueError, match="candidate"):
        asyncio.run(model.select_issue({}, ["duplicate_charge"], []))
