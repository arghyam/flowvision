# Same layout as FlowVision's image (venv builder stage, PYTHONPATH=/app/src, non-root user, gunicorn + uvicorn
# workers), without the OpenCV/PyTorch dependencies: the reading is done by the model provider's API (OpenAI or Anthropic).
#
#   docker build -t meter-reader .                  # the service (the last stage, `runtime`, is the default target)
#   docker build --target test -t meter-reader-test . && docker run --rm meter-reader-test   # runs the tests
# Configuration comes from environment variables (see .env.example and docs/DEPLOY_AWS.md).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# =========================
# Builder Stage
# =========================
FROM ${PYTHON_IMAGE} AS builder

ENV VIRTUAL_ENV=/opt/venv \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN python -m venv $VIRTUAL_ENV

WORKDIR /app
# Install from the hashed lock (regenerate it from requirements.txt; the command is at the top of that file)
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock

# =========================
# Base: the runtime environment shared by the test and runtime stages
# =========================
FROM ${PYTHON_IMAGE} AS base

COPY --from=builder /opt/venv /opt/venv
# MALLOC_ARENA_MAX=2: with many threads, glibc otherwise keeps hundreds of MB per worker after a burst of photos
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    METER_LOG_TO_FILES=false \
    MALLOC_ARENA_MAX=2

# A numeric UID/GID, so Kubernetes can verify runAsNonRoot. The code stays root-owned (the service cannot change it);
# only data/ and logs/ are writable, and only used for local photos or log files (off by default in the image).
RUN groupadd --gid 10001 flowuser && useradd --create-home --uid 10001 --gid 10001 flowuser

WORKDIR /app
COPY src ./src
COPY gunicorn.conf.py ./
RUN install -d -o 10001 -g 10001 /app/data /app/logs

# =========================
# Test Stage (not deployed): docker build --target test
# =========================
FROM base AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY scripts ./scripts
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# =========================
# Runtime Stage (the default target)
# =========================
FROM base AS runtime
USER 10001:10001
EXPOSE 8000

# Liveness only: /health answers even when every read slot is busy. /ready also checks the database.
# (ECS ignores this; declare the same check in the task definition, see docs/DEPLOY_AWS.md.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "routes:app"]
