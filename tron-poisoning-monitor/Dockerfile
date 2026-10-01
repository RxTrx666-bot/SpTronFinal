FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY data ./data
COPY pyproject.toml README.md ./

# Unprivileged user; reports are written to /app/output (mounted volume).
RUN useradd --create-home --uid 10001 monitor && mkdir -p /app/output && chown -R monitor /app/output
USER monitor

ENV HEARTBEAT_FILE=/tmp/tron-poisoning-monitor.heartbeat \
    OUTPUT_DIR=/app/output

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -m app.main health || exit 1

CMD ["python", "-m", "app.main"]
