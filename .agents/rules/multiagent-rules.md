---
description: Multi-agent coordination rules, A2A envelope protocol, and trace emission invariants.
always_on: true
---

# Multi-Agent Coordination Rules

1. **Least-Privilege Tool Access**: Only domain specialists are permitted to execute MCP tools corresponding to their domain.
2. **Deterministic Envelopes**: All A2A message transfers must be structured with `case_id`, `sender`, `recipient`, `intent`, and `payload`.
3. **Loop & Deadlock Prevention**: Hop counts must not exceed 8 hops per case. If an actor is revisited with no new evidence, terminate and escalate to coordinator.
4. **Observable Trace Logging**:
   - Always emit `task_assigned` when assigning work to a specialist.
   - Always emit `tool_result_consumed` when incorporating MCP evidence into a finding.
   - Always emit `handoff` when passing context to another actor.
   - Always emit `verification_completed` before finalizing.
5. **No Secret Prompts / CoT in Trace**: Never emit internal chain-of-thought or proprietary prompts into `traces/trace.jsonl`.
