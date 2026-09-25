---
name: gemini-model
description: Use when integrating Google Gemini models (Gemini 2.5 Pro/Flash, 1.5 Pro/Flash) via the google-genai SDK, implementing structured JSON outputs, function calling, system instructions, thinking configuration, or multimodal prompting.
---

# Google Gemini Model Integration Guide

Production guide for developing robust, high-performance applications with Google Gemini models using the unified `google-genai` SDK.

## When to Use This Skill

- Interfacing with Gemini models (`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.0-flash-lite`).
- Implementing guaranteed structured outputs using JSON schemas and Pydantic models.
- Configuring tool/function calling for agent workflows.
- Tuning system instructions, temperature, and thinking budgets (`thinking_config`).
- Implementing async streaming and concurrency-safe LLM pipelines.
- Handling rate limits, exponential backoff, and token optimization.

---

## 1. Model Matrix & Selection Guide

| Model Slug | Latency | Reasoning Tier | Best For |
| :--- | :--- | :--- | :--- |
| **`gemini-2.5-flash`** | Ultra-Fast (~300ms) | Strong | Default agent worker, entity resolution, data extraction, tool routing |
| **`gemini-2.5-pro`** | Moderate (~1-2s) | Maximum | Complex conflict resolution, nuanced policy interpretation, root-cause synthesis |
| **`gemini-2.0-flash-lite`**| Sub-200ms | Efficient | High-throughput classification, candidate pre-filtering, token reduction |

---

## 2. SDK Initialization & Async Client

Always use the modern `google-genai` SDK with async support (`client.aio`):

```python
import os
from google import genai
from google.genai import types

def get_gemini_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is required")
    return genai.Client(api_key=api_key)
```

---

## 3. Strict Structured Outputs with Pydantic

To eliminate parsing errors, pass a Pydantic schema directly via `response_schema` and set `response_mime_type="application/json"`.

```python
from pydantic import BaseModel, Field
from typing import Literal

class DisputeVerdict(BaseModel):
    case_id: str
    decision: Literal["refund_full", "refund_partial", "reject_claim", "escalate"]
    refund_amount: float = Field(ge=0.0, description="Approved refund amount in BRL")
    primary_reason: str = Field(description="Deterministic rationale citing policy clause")
    evidence_refs: list[str] = Field(description="Audit reference codes used in this decision")
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated confidence score")

async def extract_structured_verdict(
    client: genai.Client,
    prompt: str,
    system_instruction: str,
) -> DisputeVerdict:
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.0,  # Zero temperature for deterministic evaluation
        response_mime_type="application/json",
        response_schema=DisputeVerdict,
    )

    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=config,
    )

    # Automatically deserializes into validated Pydantic model
    return DisputeVerdict.model_validate_json(response.text)
```

---

## 4. Function Calling / Tool Declarations

Define tools using standard Python functions or explicit `types.FunctionDeclaration`:

```python
search_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="lookup_customer_order",
            description="Fetch verified order history and tracking status for a customer ID",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "customer_id": types.Schema(type="STRING", description="UUID of the customer"),
                    "case_id": types.Schema(type="STRING", description="Dispute case identifier"),
                },
                required=["customer_id", "case_id"],
            ),
        )
    ]
)

config = types.GenerateContentConfig(
    tools=[search_tool],
    tool_config=types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingMode.AUTO
        )
    ),
)
```

---

## 5. Thinking Budget Configuration

For Gemini 2.5 thinking-enabled models, you can control the reasoning budget:

```python
# To enable or limit extended thinking for complex logic
config = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(
        thinking_budget=1024  # Max thinking tokens, or 0 to minimize latency
    )
)
```

---

## 6. Resilience, Retries & Error Handling

```python
import asyncio
from google.genai.errors import APIError

async def call_gemini_with_retry(client, model, contents, config, max_retries=3):
    base_delay = 1.0
    for attempt in range(max_retries):
        try:
            return await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except APIError as e:
            if e.code in (429, 503) and attempt < max_retries - 1:
                sleep_time = base_delay * (2 ** attempt)
                await asyncio.sleep(sleep_time)
            else:
                raise
```
