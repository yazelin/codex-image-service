# codex-image-service

A small FastAPI wrapper around [Codex CLI](https://github.com/openai/codex)'s
built-in `$imagegen` tool. Issue bearer API keys to internal scripts, CI
jobs, and side projects so they all share **one ChatGPT subscription's
image-gen quota** over a clean HTTP endpoint, instead of each one shelling
into the host or burning separate OpenAI Images API credits.

📖 Polished guide: **[yazelin.github.io/codex-image-service](https://yazelin.github.io/codex-image-service/)**
([繁中](https://yazelin.github.io/codex-image-service/zh-tw.html))

> **Disclaimer — personal / experimental use only**
>
> This project was built for our own development and testing inside a private
> homelab. It is **not affiliated with, endorsed by, or supported by
> OpenAI**. It wraps the official `@openai/codex` CLI and re-exposes the
> CLI's `$imagegen` skill as a small HTTP API; every request consumes quota
> from the single ChatGPT account whose `~/.codex/auth.json` is mounted
> into the container.
>
> - Multi-tenanting a single ChatGPT login is not an OpenAI-documented
>   pattern — make sure your account's terms of service allow your usage
>   scenario; you are responsible for compliance, billing, and abuse
>   handling.
> - Codex CLI updates can change `$imagegen`, the model behind it
>   (`gpt-image-2`), the sandbox flags, or the on-disk layout at any time.
>   This service may need follow-up patches when that happens.
> - **No SLA, no warranty, no production hardening guarantees.** The admin
>   login is a single password + HMAC cookie; API keys are stored as sha256
>   hashes; there is no per-key rate limit, quota, audit log, or scoping
>   beyond enable / disable / delete.
> - If you fork it, audit `app/services/codex_image.py` (it runs codex with
>   `--dangerously-bypass-approvals-and-sandbox` because bubblewrap doesn't
>   work inside Docker) and re-think the threat model before pointing it at
>   anything important.

## What it gives you

- `POST /v1/images/generate` — bearer-auth, sync, returns image URLs.
- `GET /generated/<id>.png` — public download for the generated PNGs.
- `GET /health` — `{"status":"ok"}`.
- Admin UI under `/admin` for issuing / disabling / deleting API keys,
  running test generations, and manual cleanup.
- SQLite-backed history of every request with prompt, stdout, stderr,
  status, and auto-expiry by `IMAGE_RETENTION_DAYS` (default 7).

## Prerequisites

1. [Codex CLI](https://github.com/openai/codex) installed on the host that
   will run the container, and `codex login` completed.
2. Docker + Docker Compose.
3. A reverse proxy in front of the container (e.g. nginx) terminating HTTPS
   for the domain you want to expose.

## Quickstart — local testing (no nginx)

The fastest way to kick the tires. Maps port 8000 directly to your host;
no reverse proxy required.

```bash
git clone https://github.com/yazelin/codex-image-service
cd codex-image-service

cp .env.example .env
# Set at minimum:
#   ADMIN_PASSWORD          long random string
#   ADMIN_SESSION_SECRET    long random string
# Leave PUBLIC_BASE_URL and ADMIN_URL_PREFIX at their defaults.

docker compose -f docker-compose.local.yml up -d --build
```

Then:

```bash
curl -sf http://localhost:8000/health     # {"status":"ok"}
open http://localhost:8000/admin           # log in, create a key
```

Skip Docker entirely (fastest dev loop):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit as above
uvicorn app.main:app --reload --port 8000
```

This uses the host's own `~/.codex/auth.json` directly, no bind-mount.

## Production — behind your existing nginx

For a multi-caller deployment you probably want a reverse proxy terminating
TLS and serving the service at a path on an existing domain.

```bash
cp .env.example .env
# Set at minimum:
#   ADMIN_PASSWORD, ADMIN_SESSION_SECRET     long random strings
#   PUBLIC_BASE_URL                          https://images.example.com/codex-image
#   ADMIN_URL_PREFIX                         /codex-image

docker compose up -d --build
```

The default `docker-compose.yml` attaches the container to a pre-existing
Docker network called `nginx_bridge_network`. Front it with your nginx using
the snippet at `deploy/nginx.codex-image-service.location.conf.example`,
then reload nginx and verify:

```bash
curl -sf https://images.example.com/codex-image/health
# {"status":"ok"}
```

Open `https://images.example.com/codex-image/admin`, log in, click
**Create API Key**, and copy the `cimg_<random-token>` value. Refresh
or leave the page and the raw value is gone forever — only the sha256
hash stays on the server.

## Use

```bash
curl -sS --fail --max-time 650 \
  -X POST https://images.example.com/codex-image/v1/images/generate \
  -H "Authorization: Bearer $CODEX_IMAGE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a clean product photo of a ceramic tea cup","size":"1024x1024","quality":"medium","count":1}'
```

Response:

```json
{
  "id": "img_3f81...",
  "status": "succeeded",
  "images": [
    {"url": "https://images.example.com/codex-image/generated/img_3f81....png",
     "expires_at": "2026-05-27T..."}
  ],
  "created_at": "2026-05-20T..."
}
```

Python, GitHub Actions, and full deployment details live on the
[Pages site](https://yazelin.github.io/codex-image-service/).

## Queue behavior

Requests are enqueued internally; background workers run `codex exec`. The
HTTP request stays open until the image is ready or
`REQUEST_WAIT_TIMEOUT_SECONDS` (default 600) elapses. Concurrency is
controlled by `CODEX_WORKER_CONCURRENCY` (default 2). Queue depth is
capped at `GENERATION_QUEUE_MAX_SIZE` (default 50).

## Cleanup

Each `image_requests` row expires at `created_at + IMAGE_RETENTION_DAYS`.
A background sweep runs on startup and every `CLEANUP_INTERVAL_HOURS`,
deleting the PNG under `static/generated/`, the workdir under
`data/codex-runs/<id>/`, and marking the row `expired`. The admin can also
trigger immediate cleanup or per-row Delete from the dashboard.

## Version control

The repo intentionally ignores runtime state:

- `.env`
- SQLite database files under `data/`
- generated images under `static/generated/`
- Codex run directories under `data/codex-runs/`

## License

MIT
