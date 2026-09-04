# PAYVRA API. One image serves the web process, the migrations and the scripts --
# they share an interpreter and a dependency set, and three images would only create
# three chances for them to drift apart.
#
# 3.12 is the deploy target (ADR-002). psycopg[binary], rapidfuzz and openpyxl all ship
# wheels, so slim needs no compiler.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY api/ ./api/

RUN pip install --upgrade pip && pip install -e .

# **The working directory is api/, and both of the paths that matter depend on it.**
#
#   `python -m scripts.run_batch`  resolves only from here -- scripts/ is not a package
#                                  on the path from the repo root.
#   `app.config`                   walks parents[2] from app/config.py to find the repo
#                                  root, which lands on /app. That is where .env would be
#                                  read from if one existed; in this image none does, and
#                                  settings arrive as environment variables instead.
#
# alembic.ini also lives here and uses %(here)s, so `alembic -c alembic.ini` works.
WORKDIR /app/api

EXPOSE 8000

# No --reload: it doubles the process count and watches a filesystem that is baked in.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
