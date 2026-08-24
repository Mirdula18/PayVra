# ADR-007 — Postgres + APScheduler, not Celery

**Status:** Accepted
**Date:** 2026-08-23

## Context

PAYVRA is scheduler-heavy. Seven recurring jobs plus per-invoice scheduled follow-ups. The obvious
production answer is Celery with Redis. The hackathon answer may be different.

## Decision

**Database:** PostgreSQL 16 (Neon free tier), SQLAlchemy 2.0, Alembic migrations.

**Scheduler:** APScheduler running **in-process** with FastAPI, with job state persisted in Postgres
via `SQLAlchemyJobStore`.

**No Redis, no Celery, no separate worker process for MVP.**

## Rationale

**Why Postgres:** the data model is deeply relational (invoices → actions → messages → promises),
transactional integrity is essential (settle + revoke must be atomic), JSONB covers gate verdicts
and audit inputs without a second store, and Neon's free tier is sufficient.

**Why APScheduler over Celery:** Celery means Redis plus a worker process plus a beat process —
three more things to configure, deploy, and debug, and three more things that can be broken at 2am
before a demo. Our workload is low-volume, scheduled, and single-tenant-ish. APScheduler with a
Postgres job store gives persistence across restarts and runs inside the API process.

This is a **deliberate hackathon simplification**, documented as such rather than pretended to be
a production choice.

**Why persist job state:** an in-memory scheduler loses scheduled follow-ups on restart. Render
and Railway restart containers. Losing a promise-to-pay follow-up is a product failure.

## Known limits

| Limit | Threshold | Migration path |
|---|---|---|
| Single process — scheduler dies with the API | Any real deployment | Move to Celery + Redis |
| No horizontal scaling of job execution | ~5,000 invoices/day | Celery workers |
| Long jobs block the event loop | Batch > 1,000 invoices | Run in a thread pool, then Celery |
| No retry/dead-letter semantics | Production | Celery with `acks_late` |

Migration trigger: **more than one merchant in production, or batches over 1,000 invoices.**
The job functions in `scheduler/jobs.py` are written as plain callables taking `merchant_id`, so
they port to Celery tasks with a decorator and no logic change. Keep them that way.

## Concurrency requirements

Even single-process, jobs must be safe:

- `dispatch_window` claims actions with `SELECT ... FOR UPDATE SKIP LOCKED`
- `plan_day` is unique on `(invoice_id, date)` — a second run updates, never duplicates
- Settle and revoke happen in **one transaction**
- Every job is idempotent; a double-run must never double-send

## Alternatives considered

**Celery + Redis from day one.** The right production answer. Rejected for MVP: three extra
deployment units and a materially higher chance of a broken demo, for scale we do not have.

**Supabase (Postgres + auth + realtime).** Tempting for the free auth. Rejected: we do not need
realtime, and Neon's Postgres experience is cleaner for migrations.

**SQLite.** Rejected: no JSONB parity, weaker concurrency, and it does not survive a container
restart on ephemeral filesystems.

**Cron on the host.** Rejected: no persistence of per-invoice scheduled follow-ups, no visibility.

## Consequences

**Good:** One process to deploy and debug; job state survives restarts; free-tier compatible;
port to Celery is mechanical

**Bad:** Does not scale horizontally; scheduler and API share a failure domain; long jobs can block

**Required:** a `GET /health` endpoint reporting `scheduler.running` and `next_dispatch`, so a dead
scheduler is visible rather than silent.
