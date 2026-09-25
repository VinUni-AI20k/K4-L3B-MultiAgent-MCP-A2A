---
name: google-adk
description: Use when building AI agents with Google Agent Development Kit (ADK) / Agent SDK for Python, defining agent hierarchies, configuring tool registries, wiring delegation workflows, managing session state, or integrating Vertex AI / GenAI models.
---

# Google Agent Development Kit (ADK)

Production reference for building, orchestrating, and testing modular AI agents using the Google Agent Development Kit (ADK) and Google GenAI Python SDK.

## When to Use This Skill

- Creating agentic architectures with Google ADK / Agent SDK.
- Defining declarative agents with typed tool registries and system instructions.
- Wiring parent-child delegation workflows and subagent handoffs.
- Managing session state, conversation history, and ephemeral execution memories.
- Hooking agents into Vertex AI or Google GenAI (`google-genai`) model backends.
- Writing automated integration and regression tests for ADK agents.

---

## 1. Architectural Foundations of Google ADK

Google ADK structures agent applications around four core pillars:

```text
┌─────────────────────────────────────────────────────────────┐
│                       Agent Runner                          │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │                     Agent                           │   │
│   │  - System Prompt / Instructions                     │   │
│   │  - Gemini Model Configuration                       │   │
│   │  - Registered Tools (Schema Auto-Reflection)        │   │
│   │  - Subagents / Delegation Targets                   │   │
│   └──────────────────────────┬──────────────────────────┘   │
│                              │                              │
│         ┌────────────────────┴────────────────────┐         │
│         ▼                                         ▼         │
│   ┌───────────┐                             ┌───────────┐   │
│   │ Tool Call │                             │ Delegation│   │
│   │ Execution │                             │ (Subagent)│   │
│   └───────────┘                             └───────────┘   │
│                              │                              │
│                              ▼                              │
│                     Session & State Store                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Core Agent Definition Pattern

```python
from typing import Any
from google import genai
from google.genai import types

class AgentConfig:
    def __init__(
        self,
        name: str,
        instructions: str,
        model_name: str = "gemini-2.5-flash",
        tools: list[Any] | None = None,
        subagents: list["AgentConfig"] | None = None,
    ):
        self.name = name
        self.instructions = instructions
        self.model_name = model_name
        self.tools = tools or []
        self.subagents = {sa.name: sa for sa in (subagents or [])}

class ADKAgentRunner:
    def __init__(self, client: genai.Client, config: AgentConfig):
        self.client = client
        self.config = config

    async def execute(self, user_prompt: str, context: dict[str, Any] | None = None) -> str:
        """Run single-turn or multi-turn execution with tool reflection."""
        tool_declarations = [
            types.Tool(function_declarations=[tool.to_function_declaration()])
            for tool in self.config.tools
            if hasattr(tool, "to_function_declaration")
        ]

        generate_config = types.GenerateContentConfig(
            system_instruction=self.config.instructions,
            tools=tool_declarations if tool_declarations else None,
            temperature=0.1,  # Low temperature for deterministic investigation
        )

        response = await self.client.aio.models.generate_content(
            model=self.config.model_name,
            contents=user_prompt,
            config=generate_config,
        )
        return response.text or ""
```

---

## 3. Tool Registration & Schema Contracts

Tools in ADK should be cleanly typed functions with docstrings that the model can inspect.

```python
from pydantic import BaseModel, Field

class OrderLookupInput(BaseModel):
    order_id: str = Field(description="The unique UUID of the customer order")
    case_id: str = Field(description="The active dispute case ID for audit correlation")

class ADKTool:
    """Wraps an async callable into an ADK-compliant tool."""
    def __init__(self, name: str, description: str, func, input_model: type[BaseModel]):
        self.name = name
        self.description = description
        self.func = func
        self.input_model = input_model

    async def execute(self, **kwargs) -> dict[str, Any]:
        # Validate arguments against Pydantic schema before invocation
        validated = self.input_model(**kwargs)
        return await self.func(**validated.model_dump())
```

---

## 4. Multi-Agent Delegation & Handoffs

In Google ADK, agents delegate work either hierarchically (Master -> Subagent) or horizontally (Peer-to-Peer Handoff).

### Delegation Pattern:
1. **Delegation Tool**: Expose a `delegate_to_specialist(specialist_name, task_payload)` tool to the coordinator.
2. **Context Passing**: Forward the conversation history and shared state bundle to the child agent.
3. **Result Unwrapping**: The child agent's synthesized response is returned as the tool output back into the coordinator's reasoning context.

```python
async def delegate_to_specialist(agent_name: str, case_id: str, subtask: str) -> dict[str, Any]:
    """Coordinates handoff to specialist agents with bounded execution."""
    specialist = specialist_registry.get(agent_name)
    if not specialist:
        return {"error": f"Unknown specialist {agent_name}"}

    result = await specialist.run(case_id=case_id, task=subtask)
    return {"status": "success", "agent": agent_name, "findings": result}
```

---

## 5. Production Best Practices with ADK

1. **State Isolation**: Do not share mutable global state across concurrent agent executions. Always scope state to a specific `session_id` or `case_id`.
2. **Step Budgeting**: Guard agent loops with a `max_steps` counter (typically 5 to 10 iterations) to avoid runaway API consumption.
3. **Structured Outputs**: When expecting final verdicts, use Gemini's native `response_mime_type="application/json"` with a Pydantic schema rather than parsing markdown code fences.
4. **Error Boundaries**: Tool execution failures should return formatted error dictionaries rather than raising unhandled exceptions into the LLM conversation loop.
