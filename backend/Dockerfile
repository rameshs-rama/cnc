# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build dependencies for numpy wheels on architectures without a prebuilt wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install ".[postgres]"

# The service runs unprivileged; uploads are untrusted input (PRD 7.3).
RUN useradd --create-home --uid 10001 mip \
 && mkdir -p /data/object-store \
 && chown -R mip:mip /app /data
USER mip

ENV MIP_OBJECT_STORE=/data/object-store \
    MIP_DATABASE_URL=sqlite:////data/mip.db

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
