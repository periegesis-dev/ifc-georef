# syntax=docker/dockerfile:1.7

# ---- shared base: system libs + Python dependencies (cached until requirements change)
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# The pip cache mount keeps downloaded wheels between builds, so changing
# requirements.txt does not re-download everything (helps on slow connections).
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 --timeout 60 -r requirements.txt

# ---- test image: app + pytest  (docker compose --profile test run --rm tests)
FROM base AS test
COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 --timeout 60 -r requirements-dev.txt
COPY . .
ENV DATA_DIR=/tmp/ifc-georef-test
CMD ["python", "-m", "pytest", "-q"]

# ---- runtime image (default target)
FROM base AS runtime

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY --chown=app:app . .

ENV HOST=0.0.0.0 \
    PORT=5002 \
    DATA_DIR=/app/data

USER app
EXPOSE 5002

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5002/healthz', timeout=4)" || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
