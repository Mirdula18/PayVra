# Agent Instructions

Task-scoped instruction files for Claude Code. Load the one matching what you are building.

**Always read `/CLAUDE.md` first.** These files assume it.

| File | Load when working on |
|---|---|
| [backend.md](./backend.md) | FastAPI, ingestion, scoring, API endpoints |
| [agent-engine.md](./agent-engine.md) | LangGraph loop, diagnosis, guardrails, generation |
| [razorpay-integration.md](./razorpay-integration.md) | Payment links, webhooks, reconciliation |
| [frontend.md](./frontend.md) | React dashboard, worklist, audit viewer |
| [data-and-seed.md](./data-and-seed.md) | Schema, migrations, synthetic demo data |

Usage: `claude "read agents/agent-engine.md and implement the guardrail gate"`
