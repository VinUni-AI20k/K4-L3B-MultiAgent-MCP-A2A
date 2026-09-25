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


class GroqIssueModel:
    """Choose only among evidence-backed issues with Groq ALLaM-2-7B."""

    MODEL = "allam-2-7b"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("GROQ_API_KEY", "")
        if not self.api_key.strip():
            raise ValueError("GROQ_API_KEY is required to run ALLaM-2-7B")
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
                "evidence": summaries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "model": self.MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Select the best primary_issue from candidate_issues using only the "
                        "evidence. Reply with a JSON object containing primary_issue."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 64,
        }
        try:
            async with httpx2.AsyncClient(timeout=60, transport=self.transport) as client:
                response = await client.post(
                    self.URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                decision = json.loads(content)
        except (httpx2.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Groq ALLaM-2-7B request failed or returned invalid JSON") from exc
        issue = decision.get("primary_issue") if isinstance(decision, dict) else None
        if issue not in candidates:
            raise ValueError("Groq selected an issue outside the evidence candidates")
        return issue
