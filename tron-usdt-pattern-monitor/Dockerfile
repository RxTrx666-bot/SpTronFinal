# TRON USDT test-transfer pattern monitor (read-only)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HEARTBEAT_FILE=/tmp/tron-usdt-monitor.heartbeat

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY . .

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --home /app monitor && chown -R monitor /app
USER monitor

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -m app.healthcheck || exit 1

CMD ["python", "-m", "app.main", "run"]
