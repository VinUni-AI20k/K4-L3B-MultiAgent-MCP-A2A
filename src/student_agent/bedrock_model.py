from __future__ import annotations

import json
import os
from typing import Any

import httpx2

from .workflow import ISSUES, PAYMENT_VERDICTS, SHIPMENT_VERDICTS

SAFE_DOMAINS = {
    "order",
    "item",
    "payment",
    "shipment",
    "seller",
    "customer",
    "product",
    "refund",
    "policy",
}
SAFE_LABELS = ISSUES | PAYMENT_VERDICTS | SHIPMENT_VERDICTS
LABEL_FIELDS = {
    "issue_code",
    "primary_issue",
    "verdict",
    "shipment_verdict",
    "payment_verdict",
    "refund_verdict",
}


def _labels(value: Any) -> list[str]:
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in LABEL_FIELDS and isinstance(child, str) and child in SAFE_LABELS:
                    found.append(child)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return list(dict.fromkeys(found))[:10]


class BedrockIssueModel:
    """Choose an evidence-backed issue with Meta Llama 3.1 8B on Bedrock."""

    MODEL = "us.meta.llama3-1-8b-instruct-v1:0"

    def __init__(
        self,
        api_key: str | None = None,
        region: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")
        self.region = region if region is not None else (
            os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION", "")
        )
        if not self.api_key.strip():
            raise ValueError("AWS_BEARER_TOKEN_BEDROCK is required for Bedrock")
        if not self.region.strip():
            raise ValueError("AWS_DEFAULT_REGION is required for Bedrock")
        self.transport = transport

    async def select_issue(
        self, case: dict[str, Any], candidates: list[str], evidence: list[dict[str, Any]]
    ) -> str:
        summaries = [
            {"domain": item["domain"], "labels": _labels(item.get("data"))}
            for item in evidence[:10]
            if item.get("domain") in SAFE_DOMAINS
        ]
        prompt = json.dumps(
            {
                "claim_topics": [
                    claim.get("topic")
                    for claim in case.get("customer_request", {}).get("claims", [])
                    if isinstance(claim, dict)
                    and claim.get("topic") in ISSUES | {"requested_full_refund"}
                ],
                "candidate_issues": candidates,
                "evidence_labels": summaries,
                "instruction": (
                    "Choose the best supported candidate issue. Return only a JSON object "
                    'with the key "primary_issue".'
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": 64, "temperature": 0},
        }
        url = (
            f"https://bedrock-runtime.{self.region}.amazonaws.com/model/"
            f"{self.MODEL}/converse"
        )
        try:
            async with httpx2.AsyncClient(timeout=60, transport=self.transport) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                parts = response.json()["output"]["message"]["content"]
                content = "".join(part.get("text", "") for part in parts)
                decision = json.loads(content)
        except (httpx2.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            message = "Bedrock Llama 3.1 8B request failed or returned invalid JSON"
            raise RuntimeError(message) from exc
        issue = decision.get("primary_issue") if isinstance(decision, dict) else None
        if issue not in candidates:
            raise ValueError("Bedrock selected an issue outside the evidence candidates")
        return issue
