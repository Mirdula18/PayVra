# Architecture Decision Records

Each ADR captures one decision, the alternatives considered, and the consequences.

**If you change a decision, update its ADR. Do not just change the code.**

Status values: `Accepted` · `Superseded by ADR-XXX` · `Deprecated`

| ADR | Decision | Status |
|---|---|---|
| [001](./ADR-001-architecture-style.md) | Guardrailed agent loop, not a free-roaming agent | Accepted |
| [002](./ADR-002-tech-stack.md) | Python/FastAPI backend, React frontend | Accepted |
| [003](./ADR-003-llm-provider.md) | LiteLLM over Groq + Gemini free tiers, no GPU | Accepted |
| [004](./ADR-004-agent-framework.md) | LangGraph over raw tool-calling or CrewAI | Accepted |
| [005](./ADR-005-guardrails-and-compliance.md) | Deterministic gate, seven ordered checks | Accepted |
| [006](./ADR-006-razorpay-integration.md) | Payment Links as primary rail; REST over MCP | Accepted |
| [007](./ADR-007-database-and-queue.md) | Postgres + APScheduler, not Celery | Accepted |
| [008](./ADR-008-scoring-engine.md) | Explainable weighted rules over a trained model | Accepted |
