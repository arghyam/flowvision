# =========================
# Builder Stage
# =========================
FROM python:3.12-slim AS builder

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglx-mesa0 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    git \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv $VIRTUAL_ENV

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# =========================
# Final Runtime Stage
# =========================
FROM python:3.12-slim

# OpenCV runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglx-mesa0 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

ENV PYTHONPATH=/app/src

RUN useradd -m flowuser
USER flowuser

WORKDIR /app
COPY --chown=flowuser:flowuser . .

EXPOSE 8000

# Safe for CPU or GPU; still override with -e WORKERS=... -e TIMEOUT=...
CMD ["sh","-c","\
# --- CPU (respect cgroups) ---
if [ -f /sys/fs/cgroup/cpu.max ]; then \
  read QUOTA PERIOD < /sys/fs/cgroup/cpu.max; \
  if [ \"$QUOTA\" != \"max\" ] && [ -n \"$PERIOD\" ] && [ \"$PERIOD\" -gt 0 ] 2>/dev/null; then \
    CPU=$(( (QUOTA + PERIOD - 1) / PERIOD )); \
  else CPU=$(nproc 2>/dev/null || echo 1); fi; \
elif [ -f /sys/fs/cgroup/cpu/cpu.cfs_quota_us ] && [ -f /sys/fs/cgroup/cpu/cpu.cfs_period_us ]; then \
  QUOTA=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null || echo -1); \
  PERIOD=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us 2>/dev/null || echo 100000); \
  if [ \"$QUOTA\" -gt 0 ] && [ \"$PERIOD\" -gt 0 ]; then \
    CPU=$(( (QUOTA + PERIOD - 1) / PERIOD )); \
  else CPU=$(nproc 2>/dev/null || echo 1); fi; \
else CPU=$(nproc 2>/dev/null || echo 1); fi; \
[ \"$CPU\" -lt 1 ] && CPU=1; \
\
# --- GPU count (NVIDIA_VISIBLE_DEVICES or nvidia-smi) ---
if [ -n \"${NVIDIA_VISIBLE_DEVICES:-}\" ] && [ \"${NVIDIA_VISIBLE_DEVICES}\" != \"none\" ]; then \
  cnt=0; IFS=,; set -- ${NVIDIA_VISIBLE_DEVICES}; \
  for d; do d=$(echo \"$d\" | tr -d '[:space:]'); [ -n \"$d\" ] && [ \"$d\" != \"void\" ] && cnt=$((cnt+1)); done; \
  unset IFS; GPU=$cnt; \
elif command -v nvidia-smi >/dev/null 2>&1; then \
  GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l | tr -d ' '); \
else GPU=0; fi; \
\
# --- Preference & overrides ---
PREF=${PREFER_GPU:-true}; \
case \"$PREF\" in 1|true|TRUE|True|yes|y|Y) PREFER_GPU=true ;; *) PREFER_GPU=false ;; esac; \
\
# --- WORKERS (auto unless provided) ---
if [ -z \"${WORKERS:-}\" ]; then \
  if [ \"$PREFER_GPU\" = true ] && [ \"$GPU\" -gt 0 ]; then \
    W=$GPU; [ \"$CPU\" -ge 16 ] && [ \"$GPU\" -ge 2 ] && W=$((W+1)); \
    [ \"$W\" -lt 1 ] && W=1; WORKERS=$W; \
  else \
    W=$(( (CPU + 1) / 2 )); [ \"$W\" -gt 8 ] && W=8; [ \"$W\" -lt 2 ] && W=2; WORKERS=$W; \
  fi; \
fi; \
\
# --- TIMEOUT (auto unless provided) ---
if [ -z \"${TIMEOUT:-}\" ]; then \
  if [ \"$PREFER_GPU\" = true ] && [ \"$GPU\" -gt 0 ]; then TIMEOUT=600; else TIMEOUT=300; fi; \
fi; \
\
echo \"[auto] CPU=$CPU GPU=$GPU WORKERS=$WORKERS TIMEOUT=$TIMEOUT\"; \
exec gunicorn -k uvicorn.workers.UvicornWorker routes:app \
  --bind 0.0.0.0:8000 \
  --workers \"$WORKERS\" \
  --timeout \"$TIMEOUT\" \
  --graceful-timeout \"${GRACEFUL_TIMEOUT:-60}\" \
  --keep-alive \"${KEEPALIVE:-30}\" \
  --max-requests \"${MAX_REQUESTS:-200}\" \
  --max-requests-jitter \"${MAX_REQUESTS_JITTER:-50}\" \
  --log-level \"${LOG_LEVEL:-info}\""]

