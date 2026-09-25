"""A small-model verifier using an OpenAI-compatible chat endpoint."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx2


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str
    parameters_b: float

    @classmethod
    def load(cls) -> ModelSettings:
        base_url = os.getenv("AGENT_BASE_URL", "").strip().rstrip("/")
        model = os.getenv("AGENT_MODEL", "").strip()
        api_key = os.getenv("AGENT_API_KEY", "").strip()
        raw_size = os.getenv("AGENT_MODEL_PARAMS_B", "").strip()
        try:
            parameters_b = float(raw_size)
        except ValueError as exc:
            raise ValueError(
                "AGENT_MODEL_PARAMS_B must specify the model size in billions"
            ) from exc
        if not base_url.startswith(("http://", "https://")) or not model:
            raise ValueError("AGENT_BASE_URL and AGENT_MODEL must be configured")
        if not 0 < parameters_b <= 10:
            raise ValueError("agent model must have at most 10B parameters")
        return cls(base_url, model, api_key, parameters_b)


def _json_content(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ValueError("model response has no text")
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")
    return parsed


class ModelVerifier:
    def __init__(self, settings: ModelSettings) -> None:
        self.settings = settings

    async def review(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Ask the model to select among evidence-derived candidates only.

        The customer message is intentionally omitted.  It is untrusted input
        and may contain instructions unrelated to the complaint.
        """
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        payload = {
            "model": self.settings.model,
            "temperature": 0,
            "max_tokens": 350,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You verify an e-commerce complaint. Use only the supplied facts. "
                        "Dated events from other purchase episodes must be ignored. "
                        "Select primary_issue from candidate_issues. Prefer confirmed domain "
                        "events, then status, then numeric comparisons. Return exactly one "
                        'JSON object: {"primary_issue":"...","confidence":0.0,'
                        '"secondary_issues":[]}. No explanation.'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(facts, ensure_ascii=False, separators=(",", ":")),
                },
            ],
        }
        async with httpx2.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{self.settings.base_url}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            body = response.json()
        return _json_content(body["choices"][0]["message"]["content"])
