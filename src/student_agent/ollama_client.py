from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger("student_agent.ollama")

THINK_REGEX = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_think_and_content(raw_text: str) -> tuple[str, str]:
    """Extract <think>...</think> chain of thought and clean response body."""
    thinking = ""
    think_match = THINK_REGEX.search(raw_text)
    if think_match:
        thinking = think_match.group(1).strip()
        cleaned = THINK_REGEX.sub("", raw_text).strip()
    else:
        cleaned = raw_text.strip()
    return thinking, cleaned


def parse_json_from_response(raw_text: str) -> dict[str, Any] | None:
    """Safely extract and parse JSON object from LLM response."""
    _, cleaned = extract_think_and_content(raw_text)
    
    # 1. Try markdown code block
    match = JSON_BLOCK_REGEX.search(cleaned)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 2. Try raw text
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 3. Try finding outermost { ... }
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        snippet = cleaned[first_brace : last_brace + 1]
        try:
            data = json.loads(snippet)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


class OllamaAgentClient:
    """Wrapper around Ollama chat for Orchestrator and Specialist agents."""

    def __init__(
        self,
        model: str,
        system_prompt: str,
        host: str | None = None,
        temperature: float = 0.1,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.temperature = temperature

    async def chat(
        self,
        user_prompt: str,
        *,
        history: list[dict[str, str]] | None = None,
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Call Ollama chat asynchronously.
        
        Returns:
            (thinking_text, cleaned_content, parsed_json_or_none)
        """
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_prompt})

        try:
            from ollama import chat as ollama_chat
            
            # Run in thread pool to avoid blocking the asyncio event loop
            response = await asyncio.to_thread(
                ollama_chat,
                model=self.model,
                messages=messages,
                options={"temperature": self.temperature},
            )
            raw_content = getattr(response.message, "content", "") or ""
        except ImportError:
            logger.warning("Package 'ollama' is not installed. Using heuristic fallback.")
            raw_content = ""
        except Exception as exc:
            logger.warning(f"Ollama chat call failed for model {self.model}: {exc}")
            raw_content = ""

        thinking, content = extract_think_and_content(raw_content)
        parsed = parse_json_from_response(raw_content) if raw_content else None
        return thinking, content, parsed
