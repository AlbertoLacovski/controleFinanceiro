FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt \
    && npm install --global @openai/codex
ENV PATH="/opt/venv/bin:${PATH}"
COPY app.py /app/app.py
COPY templates /app/templates
COPY static /app/static
RUN mkdir -p /app/runtime/codex-home /app/runtime/codex-workspace
ENV FINANCE_DATA_DIR=/app/runtime \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    COOKIE_SECURE=true \
    CODEX_HOME=/app/runtime/codex-home \
    WPP_BASE_URL=http://wppconnect:21465 \
    TZ=America/Sao_Paulo
EXPOSE 8000
CMD ["python3", "/app/app.py"]
