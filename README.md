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

## Admin dashboard

Short live walk-through of the admin UI — API key management, request history with
prompt + stdout + stderr, manual cleanup, and the test-generation form:

<video src="https://github.com/yazelin/codex-image-service/raw/main/examples/admin-dashboard.mp4" controls width="700" muted playsinline></video>

If your viewer doesn't render the inline player (some markdown renderers
don't), the raw MP4 lives at
[`examples/admin-dashboard.mp4`](./examples/admin-dashboard.mp4) (19 s, 1920×1200, 2.4 MB).

## What it gives you

- `POST /v1/images/generate` — bearer-auth, sync, returns image URLs.
- `POST /v1/images/jobs` — bearer-auth, async: returns `202` with a job id
  immediately, no long-lived connection needed.
- `GET /v1/images/jobs/<id>` — poll job status until `succeeded` / `failed`.
- `POST /v1/vision` — bearer-auth, sync: send `{prompt, images_base64}` and get the
  model's final text back. The Codex CLI underneath can read images as well as make
  them; this endpoint is the read side. Useful for checking a generated image against
  a spec from CI, where no logged-in Codex CLI exists. Skips the generation queue
  (a ~20 s read should not wait behind a multi-minute render) but shares the same
  accounts and the same per-`CODEX_HOME` exec lock.
- `GET /generated/<id>.png` — public download for the generated PNGs.
- `GET /health` — `{"status":"ok"}`.
- Admin UI under `/admin` for issuing / disabling / deleting API keys,
  running test generations, and manual cleanup.
- Per-account ChatGPT quota on the overview page: each `CODEX_HOME` card shows
  the remaining percentage and reset countdown for every rate-limit window the
  ChatGPT backend reports, so a pool account running dry is visible before it
  starts failing jobs. Window names come from `limit_window_seconds` rather
  than the primary/secondary position — team plans expose a single 7-day
  window in `primary_window`, so labelling by position reads a weekly limit as
  a 5-hour one.
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

### Image edit (single or multi-image composition)

Attach 1–4 reference images as base64 strings under `reference_images_base64`.
The service runs `codex exec --image <each> -- <prompt>`, which feeds them all
to gpt-image-2 edit. Use this for outfit swaps, scene merges, "put X from
image 1 into image 2", etc.

```bash
A=$(base64 -w0 < person.png)
B=$(base64 -w0 < kitchen.png)
curl -sS --fail --max-time 650 \
  -X POST https://images.example.com/codex-image/v1/images/generate \
  -H "Authorization: Bearer $CODEX_IMAGE_KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg a "$A" --arg b "$B" '{
    prompt: "place the person from image 1 into the kitchen scene from image 2, preserve their face and outfit",
    reference_images_base64: [$a, $b],
    size: "1024x1024",
    quality: "medium"
  }')"
```

`count` is forced to 1 in edit mode (gpt-image-2 edit returns one image).
The legacy singular field `reference_image_base64: "<base64>"` still works
and is treated as a 1-element list.

Python, GitHub Actions, and full deployment details live on the
[Pages site](https://yazelin.github.io/codex-image-service/).

### Async job API (submit + poll)

Generation takes 70–180 s. The sync endpoint above holds the HTTP
connection open the whole time, which breaks behind proxies with shorter
timeouts (Cloudflare Workers, nginx defaults) — and if the proxy gives up
with a 504, the result is lost even though the image was generated. For
long-running callers, prefer the job endpoints; the sync endpoint stays
fully compatible.

Submit (returns immediately with `202`):

```bash
curl -sS --fail \
  -X POST https://images.example.com/codex-image/v1/images/jobs \
  -H "Authorization: Bearer $CODEX_IMAGE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a clean product photo of a ceramic tea cup","size":"1024x1024","quality":"medium","count":1}'
# {"id":"img_3f81...","status":"queued"}
```

The request body is identical to `/v1/images/generate`, including
`reference_images_base64` for edit mode. `503` means the queue is full —
retry later.

Poll:

```bash
curl -sS --fail \
  https://images.example.com/codex-image/v1/images/jobs/img_3f81... \
  -H "Authorization: Bearer $CODEX_IMAGE_KEY"
```

```json
{
  "id": "img_3f81...",
  "status": "succeeded",
  "images": [
    {"url": "https://images.example.com/codex-image/generated/img_3f81....png",
     "expires_at": "2026-06-17T..."}
  ],
  "error": null,
  "created_at": "2026-06-10T...",
  "expires_at": "2026-06-17T..."
}
```

`status` is one of `queued` / `running` / `succeeded` / `failed` /
`expired`. On `failed`, `error` carries the reason. Jobs are only visible
to the API key that submitted them; any other key (or an unknown id) gets
`404`.

Polling advice: every 5 s for the first ~90 s, then back off to every
10 s. Give up after ~10 min — by then the job has either finished or
failed server-side.

**Deploying this to an existing homelab instance:** the endpoints ship in
the app image, so update the checkout and rebuild:

```bash
git pull && docker compose up -d --build
```

## Queue behavior

Requests are enqueued internally; background workers run `codex exec`. On
the sync endpoint the HTTP request stays open until the image is ready or
`REQUEST_WAIT_TIMEOUT_SECONDS` (default 600) elapses; the async job
endpoint returns as soon as the job is queued. Concurrency is
controlled by `CODEX_WORKER_CONCURRENCY` (default 2). Queue depth is
capped at `GENERATION_QUEUE_MAX_SIZE` (default 50). Both endpoints share
the same queue and the same depth cap.

## Multi-account round-robin (optional)

A single ChatGPT subscription's per-account image-gen quota is the real
cap on throughput. Configure two or more ChatGPT accounts and the service
rotates `CODEX_HOME` between them per request, with automatic cross-account
retry if any one account errors out.

Each account needs its own host-side directory under `~/codex-homes/`:

```bash
mkdir -p ~/codex-homes/{personal,team}
CODEX_HOME=~/codex-homes/personal codex login   # log in with ChatGPT A
CODEX_HOME=~/codex-homes/team     codex login   # log in with ChatGPT B
```

The folder names are just labels — `personal` / `team` / `team-acme` /
`backup-account`, whatever helps you remember which is which. Two
`codex login`s on the *same* ChatGPT user account would point at the same
quota pool though, so you only get extra capacity by using genuinely
distinct user accounts.

Then add to `.env` (paths as visible inside the container):

```dotenv
CODEX_HOMES=/host_codex_homes/personal:/host_codex_homes/team
```

`docker-compose.yml` already mounts `~/codex-homes:/host_codex_homes`
read-write (codex writes sessions and rotates tokens inside `CODEX_HOME`),
so any subdirectory you create under `~/codex-homes/` becomes available
at `/host_codex_homes/<name>` inside the container. An `.env`-only change
needs just `docker compose up -d` to take effect; the rotation kicks in
once the container is recreated.

When more than one account is configured, the Overview also carries a
**Dispatch mode** switch (stored in the DB, survives restarts):

- `round-robin` (default) — every request advances to the next account, so
  usage spreads evenly and no single ChatGPT plan hits its cap first.
- `primary-first` — always start on the first account; the others only get
  used when it fails (the retry steps to the next one). Use this to keep a
  backup account's quota untouched, or when one account is on a better plan.

The admin Overview shows one card per account with a 30-day request
count, success/failure split, a **24h success rate** (the 30-day total
dilutes an account that only started failing this morning; consumers
like catime fail over to gemini on error, so a dead account still looks
like "images are coming out" from the outside — this number is the only
place it shows), auth-token freshness (green ≤6d, amber
7–9d, red ≥10d since `last_refresh`), and the first 8 chars of the
ChatGPT `account_id` so you can tell which is which. The History page
gains an Account column with the chosen home (tooltip shows the full
path).

**Token refresh maintenance:** access tokens live ~10 days (240h) and the
pool homes are mounted read-write, so codex refreshes them in place —
whichever process (a generation or the keepalive) runs first once the token
needs rotating does it. A daily keepalive keeps idle accounts warm:

```bash
# daily cron — touches each home under the same lock the service uses
0 4 * * * ~/codex-homes/refresh-tokens.sh >> ~/codex-homes/refresh-tokens.log 2>&1
```

**Never point a home at a read-only auth.json.** Codex rotates the refresh
token with the server and then writes it back; if the write can't land, the
next run presents a token the server already retired, which is reuse. Reuse
does not just kill that one home — OpenAI revokes every session belonging to
that ChatGPT **user**, so two homes logged in as the same user die together
(observed 2026-08-01: `refresh_token_invalidated` /
`"Your session has ended. Please log in again."` across both of one user's
homes within the same hour). Corollary for capacity planning: multiple homes
on one ChatGPT user share a failure domain even when they sit in different
workspaces.

**Token audit trail:** every run fingerprints the home's `auth.json` before
and after (sha256 prefix of the refresh token — never the token itself) and
appends a line to `data/token-audit.log` when it rotated, or when a run was
killed on timeout. Grep it first when accounts start failing auth:

```bash
grep '"rotated": true' data/token-audit.log | tail
```

A `timeout_kill` entry with `"rotated": true` is the dangerous case: the run
was killed across a rotation, so the home may be holding a retired token.

## Troubleshooting

**`502 Bad Gateway` + container in a restart loop, logs show
`sqlite3.OperationalError: attempt to write a readonly database` or
`Permission denied`** — this means bind-mounted host files are root-owned
from an earlier container that ran as root, but the current container
runs as your host UID (1000 by default). One-time fix:

```bash
sudo chown -R $USER:$USER ./data ./static ~/codex-homes
rm -f ./data/app.db-wal ./data/app.db-shm   # clear any stale SQLite WAL/SHM
docker compose up -d --build
```

After this, the container's uid stays in sync with your host user and
new writes preserve ownership automatically.

## Cleanup

Each `image_requests` row expires at `created_at + IMAGE_RETENTION_DAYS`.
A background sweep runs on startup and every `CLEANUP_INTERVAL_HOURS`,
deleting the PNG under `static/generated/`, the workdir under
`data/codex-runs/<id>/`, and marking the row `expired`.

The same sweep also deletes **Codex session rollouts** older than
`SESSION_RETENTION_DAYS` (default 3) from every `CODEX_HOMES` entry, and from
the container's own `~/.codex` when `CODEX_HOMES` is empty. This matters more
than it sounds: newer Codex embeds each generated image as base64 inside the
session rollout `.jsonl` rather than writing a PNG, and this service reads the
image back out of it — so every generation *necessarily* leaves behind a
rollout carrying a full copy of the image. Once extracted, that file is dead
weight, and nothing else removes it.

Left unmanaged it grows without bound. On one deployment `~/codex-homes`
reached 23 GB across three rotating accounts — 5.7 GB from a single month of
heavy generation — and was still growing by roughly 1.3 GB every three days.
Retention is short because these files are only useful for `codex resume`,
which this service never does (it runs one-shot subprocesses); the window
exists so a human can still inspect a recent failure. Set it to `0` to
disable the sweep. The admin can also
trigger immediate cleanup or per-row Delete from the dashboard.

## Version control

The repo intentionally ignores runtime state:

- `.env`
- SQLite database files under `data/`
- generated images under `static/generated/`
- Codex run directories under `data/codex-runs/`

## License

MIT
