# ADR-002 — Python/FastAPI backend, React frontend

**Status:** Accepted
**Date:** 2026-08-23

## Context

Small team, hackathon timeline, Claude Code as the primary development tool. The system needs
schema-validated LLM output, scheduled background jobs, a relational data model with strong
transactional guarantees, and a dashboard polished enough to survive judge scrutiny.

## Decision

**Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic
**Frontend:** React 18, Vite, TypeScript, Tailwind, shadcn/ui, TanStack Query, Recharts
**Deploy:** Render or Railway (API), Vercel (web), Neon (Postgres)

## Rationale

Pydantic v2 is the deciding factor. Every LLM output in this system is schema-validated before use,
and Pydantic gives that for free with structured error messages we can feed back into a repair
prompt. Doing the same in TypeScript means hand-rolling Zod schemas plus a separate validation
layer.

Python also has the strongest agent tooling (LangGraph, LiteLLM) and the strongest tabular/ML
libraries if the scoring engine graduates from rules to LightGBM.

FastAPI gives async I/O, which matters because almost every operation here is network-bound
(Razorpay, LLM, email provider).

On the frontend, shadcn/ui is chosen specifically because judge-facing polish is a scoring factor
and hand-rolling components burns hours we do not have.

## Alternatives considered

**Node/NestJS end to end.** One language, shared types. Rejected: weaker schema-validation
ergonomics for LLM output, weaker agent framework ecosystem.

**Django.** Batteries included, admin panel free. Rejected: heavier than needed, sync-first ORM
fights the network-bound workload, and the admin panel is not the UI we want judges to see.

**Next.js full-stack.** Rejected: background scheduling in serverless is awkward, and this system
is scheduler-heavy by design.

## Consequences

**Good:** Fast iteration, strong validation, good agent ecosystem, free-tier deployable
**Bad:** Two languages, two dependency trees, duplicated types between API and frontend

**Mitigation:** generate TypeScript types from the OpenAPI schema FastAPI produces automatically.
Do this in Phase 8, not before.
