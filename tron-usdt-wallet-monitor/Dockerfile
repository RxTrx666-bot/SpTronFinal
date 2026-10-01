# TRON USDT TRC-20 wallet monitor (read-only)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY . .

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --home /app monitor && chown -R monitor /app
USER monitor

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -m app.health || exit 1

CMD ["python", "-m", "app.main"]
