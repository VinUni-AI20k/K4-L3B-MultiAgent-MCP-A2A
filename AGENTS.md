# Agent Instructions for K4-L3B-MultiAgent-MCP-A2A

Welcome to the **K4 L3B — Multi-Agent MCP + A2A** repository. This codebase implements a multi-agent e-commerce customer dispute investigation system powered by Model Context Protocol (MCP) and Agent-to-Agent (A2A) orchestration.

---

## 1. Core Architecture & Components

- **Domain**: E-commerce customer dispute investigation on the Brazilian E-commerce dataset (Olist).
- **Multi-Agent Hierarchy**:
  - `Entity Resolver`: Disambiguates customers, orders, and sellers from partial/noisy candidate data.
  - `Coordinator`: Orchestrates the overall lifecycle, assigns bounded sub-tasks to specialists, and prevents circular handoffs.
  - `Order / Product Specialist`: Queries MCP order tools, validates product catalog details, and checks seller status.
  - `Shipment Specialist`: Queries MCP tracking tools, evaluates delivery timelines, logistics milestones, and carrier delays.
  - `Payment / Refund Specialist`: Analyzes transactions, installments, payment types, and existing refunds.
  - `Conflict Resolver`: Reconciles buyer complaints against seller evidence and platform policies.
  - `Verifier`: Evaluates final invariants (schema validity, evidence provenance, financial math, timeline logic).
- **Execution Seam**:
  - Primary entrypoint: `src/student_agent/workflow.py` -> `async def solve_case(case, gateway, trace) -> dict`
  - CLI: `src/student_agent/cli.py` (`day09 run`, `day09 validate`, `day09 package`)
  - Contracts & Schemas: `contracts/schemas/` (`l3b-output-v2.schema.json`, `trace-event-v1.schema.json`)

---

## 2. Hard Invariants & Scoring Rubric

The competition evaluates submissions on 8 dimensions:

| Component | Weight | Key Invariant |
| :--- | :---: | :--- |
| **Semantic Correctness** | 40% | Correct resolution decision, responsible party, and rejected candidates. |
| **Evidence Quality** | 15% | All claims grounded in verifiable facts from MCP evidence responses. |
| **Provenance Audit** | 15% | Every cited `evidence_ref` must have been queried on the server for this case. |
| **Consistency** | 10% | Refund amount $\le$ order total; delivery date $\ge$ shipping date $\ge$ purchase date. |
| **Schema Conformance** | 5% | Zero validation errors against `contracts/schemas/l3b-output-v2.schema.json`. |
| **Confidence Calibration** | 5% | Confidence reflects real evidentiary backing (never return 1.0 on ambiguous cases). |
| **Workflow Adherence** | 5% | Observable trace events (`task_assigned`, `tool_result_consumed`, `handoff`, `verification_completed`). |
| **Tool Efficiency** | 5% | Minimize wasted/unconsumed tool calls; cache within case scope. |

> [!CAUTION]
> **Zero-Point Disqualifications**:
> - Never invent or fake `evidence_ref` tokens.
> - Never reuse evidence across different cases.
> - Never log prompts or chain-of-thought in `traces/trace.jsonl` or final output.

---

## 3. Skill & Hook Tooling

This repo uses the **TypeSafe Jev Skill Selector** (`jev-skill-selector`) to automatically route relevant skills per user prompt:

- **Skills Bank**: `.agents/jev_skills/` and `~/.agents/jev_skills/`
- **Installed Skills**:
  - `multiagent-a2a-orchestration`: Coordinator-Specialist design, A2A envelopes, entity resolution.
  - `google-adk`: Google Agent Development Kit / Agent SDK architecture and runners.
  - `gemini-model`: Google Gemini 2.5 / 1.5, `google-genai` SDK, structured JSON outputs, function calling.
  - `mcp-server`: MCP client & server, `ClientSession`, streaming HTTP, audit trails.
  - `benchmark-ai-agent`: Multi-case benchmarking, calibration, scoring, failure analysis.
  - `python-testing-patterns`: Comprehensive async pytest, fixtures, mocking.
  - `pytest-coverage`: Measuring and maximizing test coverage.
  - `git-rebase-patterns`: Linear git history, interactive rebasing, conflict resolution.
  - `using-git-worktrees`: Workspace isolation for agent features.
  - `tdd`: Test-driven development (red-green-refactor).

---

## 4. Development & Testing Workflow

1. **Activate Environment**:
   ```bash
   source .venv/bin/activate
   ```
2. **Run Tests**:
   ```bash
   pytest -q
   ```
3. **Format & Lint**:
   ```bash
   ruff check src tests
   ruff format --check src tests
   ```
4. **Inspect MCP Tools**:
   ```bash
   day09 mcp-tools
   ```
5. **Validate Outputs & Trace**:
   ```bash
   day09 validate
   ```
