"""Bounded JSON generation using Qwen3 through local Ollama."""

from __future__ import annotations

import asyncio
import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


class JsonModel:
    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
        self.name = os.getenv("LLM_MODEL", "qwen3:4b")
        self.backend = os.getenv("LLM_BACKEND", "ollama")
        self.key = os.getenv("LLM_API_KEY", "")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
        self.context = int(os.getenv("LLM_CONTEXT_TOKENS", "16384"))
        size = float(os.getenv("LLM_MODEL_PARAMETERS_B", "4.02"))
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL must be an HTTP(S) URL")
        if self.backend not in {"ollama", "openai-compatible"}:
            raise ValueError("LLM_BACKEND must be ollama or openai-compatible")
        if not math.isfinite(size) or not 0 < size <= 10:
            raise ValueError("LLM_MODEL_PARAMETERS_B must be in (0, 10]")
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 900:
            raise ValueError("LLM_TIMEOUT_SECONDS must be between 1 and 900")
        if not 8192 <= self.context <= 32768:
            raise ValueError("LLM_CONTEXT_TOKENS must be between 8192 and 32768")

    @classmethod
    def load(cls, root):
        load_dotenv(root / ".env")
        return cls()

    async def complete(self, system: str, payload: dict, schema: dict | None = None) -> dict:
        return await asyncio.to_thread(self._complete, system, payload, schema)

    async def check(self) -> None:
        value = await self.complete(
            'Return exactly {"ok":true} as JSON.',
            {},
            {
                "type": "object",
                "properties": {"ok": {"const": True}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
        if value != {"ok": True}:
            raise ValueError("Model JSON smoke test failed")

    def _complete(self, system: str, payload: dict, schema: dict | None) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        if self.backend == "ollama":
            url = self.base_url.removesuffix("/v1") + "/api/chat"
            body = {
                "model": self.name,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": schema or "json",
                "options": {"temperature": 0, "num_ctx": self.context, "num_predict": 6000},
            }
        else:
            url = self.base_url + "/chat/completions"
            body = {
                "model": self.name,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 6000,
                "response_format": {"type": "json_object"},
            }
        headers = {"Content-Type": "application/json"}
        if self.key and self.backend != "ollama":
            headers["Authorization"] = f"Bearer {self.key}"
        request = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
            if self.backend == "ollama":
                if result.get("done_reason") == "length":
                    raise ValueError("Model response truncated")
                content = result["message"]["content"]
            else:
                choice = result["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("Model response truncated")
                content = choice["message"]["content"]
            value = json.loads(content)
        except HTTPError as exc:
            raise RuntimeError(f"Model HTTP {exc.code}; check model/server configuration") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("Cannot connect to model; start Ollama and pull qwen3:4b") from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Model did not return a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("Model result must be a JSON object")
        return value
