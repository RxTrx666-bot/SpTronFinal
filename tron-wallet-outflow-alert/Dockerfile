# Read-only TRON USDT outgoing-transfer alert bot
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/outflow_alert.db \
    HEARTBEAT_FILE=/tmp/outflow_alert.heartbeat

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY outflow_alert ./outflow_alert

RUN useradd --system --uid 10001 --home /app bot && mkdir -p /data && chown bot /data
USER bot
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -m outflow_alert.healthcheck || exit 1

CMD ["python", "-m", "outflow_alert"]
