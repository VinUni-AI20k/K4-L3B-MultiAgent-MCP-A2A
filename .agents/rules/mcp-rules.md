---
description: Model Context Protocol (MCP) tool usage, case scoping, evidence integrity, and efficiency invariants.
always_on: true
---

# MCP Usage Rules

1. **Tool Discovery**: Never assume tool names. Always discover available tools dynamically via `gateway.list_tools()`.
2. **Case Scoping**: Every call to `gateway.call(...)` must specify `case_id=case["case_id"]`.
3. **Evidence Token Integrity**: Never alter, mock, or invent `evidence_ref` values. Every claim must reference real `evidence_ref` tokens issued by the MCP server.
4. **Zero Cross-Case Leakage**: Evidence obtained under one `case_id` must never be used or referenced in any other case.
5. **Efficiency & Memoization**:
   - Cache tool results in memory per case.
   - Do not make redundant queries.
   - Only query tools relevant to the specific dispute scenario.
