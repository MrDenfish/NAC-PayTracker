# syntax=docker/dockerfile:1
# =============================================================================
# NAC-Pay production image — FastAPI + uvicorn
# Built for colocation on the CrewRef box behind the shared `amis-caddy`.
# Build context is the repo root:  docker build -f deploy/Dockerfile .
# =============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is only needed for the container healthcheck. Every runtime dependency
# (argon2-cffi, pdfplumber/Pillow, psycopg2-binary, uvicorn[standard]) ships
# manylinux wheels, so no compiler toolchain is required.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install the package (and its `app` extra) from source. pyproject + src are
# all hatchling needs; copying them as one layer keeps the build simple.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[app]"

# Run unprivileged. /data is the SQLite home (NAC_PAY_DATA_DIR), mounted as a
# named volume so the DB survives container rebuilds.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

ENV NAC_PAY_DATA_DIR=/data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

# Single worker: SQLite serializes writes, and traffic is low for first-real-
# world testing. Scale to `--workers N` (or Postgres) when load warrants it.
CMD ["uvicorn", "nac_pay.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
