# =========================
# Builder Stage
# =========================
FROM python:3.12-slim AS builder

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install from the hashed lock (regenerate it from pyproject.toml; see README)
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

# =========================
# Final Runtime Stage
# =========================
FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv

# Code stays root-owned and read-only for the app user: the service writes nothing
# to disk and logs to stdout. WORKDIR comes before USER on purpose; the legacy
# builder creates it as root either way.
WORKDIR /app
COPY src ./src

# Numeric UID so Kubernetes can enforce runAsNonRoot
RUN useradd --system --uid 10001 --user-group --no-create-home elmuser
USER 10001:10001

EXPOSE 8000

# One process per container; scale with replicas, not workers
CMD ["uvicorn", "elm_ocr.main:app", "--host", "0.0.0.0", "--port", "8000"]
