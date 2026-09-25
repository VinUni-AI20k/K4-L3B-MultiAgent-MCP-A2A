---
description: Test-driven development, async pytest standards, and schema validation guidelines.
always_on: true
---

# Testing Rules

1. **Test-Driven Development (TDD)**: Write failing tests at public seams before implementing features or fixing bugs.
2. **Schema Validation**: All synthetic or mocked outputs in tests must be validated against `contracts/schemas/` schemas using `jsonschema`.
3. **Async Test Practices**: Use `pytest.mark.asyncio` with explicit event loop scoping for async workflow and gateway testing.
4. **Mock Isolation**: When mocking `EvidenceGateway` or `ClientSession`, ensure the mock conforms to the actual return shapes defined in `contracts/schemas/mcp-evidence-response-v1.schema.json`.
5. **Coverage**: Aim for 100% coverage on core reasoning, entity resolution, and contract validation modules.
