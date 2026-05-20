FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git nodejs npm \
    && npm install -g @openai/codex@0.131.0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

# Create a non-root user matching the typical host UID so bind-mounted
# files (in particular ~/codex-homes/*/) stay owned by the host operator
# and remain readable from host-side scripts (token-refresh cron etc).
# Compose can override this by setting `user:` to a different UID/GID.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app \
    && useradd -m -u ${APP_UID} -g ${APP_GID} -d /home/app -s /bin/bash app

COPY app ./app
COPY static ./static

# Pre-create the runtime dirs the service writes to, and the default
# CODEX_HOME path (mounted from the host as the single-account fallback).
RUN mkdir -p /app/data /app/static/generated /root/.codex \
    && chown -R ${APP_UID}:${APP_GID} /app

USER app:app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
