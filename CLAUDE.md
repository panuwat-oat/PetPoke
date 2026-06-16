# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PetPoke polls the PetKit cloud and sends **repeating** Telegram alerts on a backoff schedule until a device problem is resolved (PetKit's own app only notifies once). Core poll logic lives in one file, `notifier.py`, by design — so the runtime platform is easy to swap.

**Runtime: Google Cloud Run + Cloud Scheduler** (project `petpoke-notifier`, region `us-central1`). Cloud Scheduler hits the service's `/poll` endpoint every 15 min on a precise schedule. The old GitHub Actions cron (`poll.yml`) is **disabled** — it drifted 20–45 min due to GHA cron lag, which was the reason for migrating. The workflow file is removed; `notifier.py` stays platform-agnostic so it can still run anywhere (local, Pi, etc.).

The README is in Thai and is the authoritative setup/operations guide. User-facing Telegram strings are intentionally Thai.

## Commands

```bash
# Local run (macOS/Linux)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a; python notifier.py   # needs .env (copy from .env.example)

# First-time field discovery: dump raw device state from PetKit
DEBUG_LOG_RAW=true python notifier.py
```

No test suite, linter, or build step exists. Python 3.11 (uses `zoneinfo`, `from __future__ import annotations`).

```bash
# Cloud Run — redeploy after code changes (Cloud Build buildpacks, no Dockerfile)
gcloud run deploy petpoke --source . --project=petpoke-notifier --region=us-central1 \
  --no-allow-unauthenticated --memory=512Mi --timeout=120 --max-instances=1

# Tail logs
gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=petpoke' \
  --project=petpoke-notifier --limit=30 --freshness=15m --format="value(timestamp,textPayload)"

# Manually fire a poll (uses the scheduler's OIDC identity)
gcloud scheduler jobs run petpoke-poll --project=petpoke-notifier --location=us-central1

# Rotate a secret value
printf %s "NEW_VALUE" | gcloud secrets versions add TELEGRAM_BOT_TOKEN --project=petpoke-notifier --data-file=-
```

Required env: `PETKIT_USERNAME`, `PETKIT_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Optional: `PETKIT_REGION` (default `TH`), `PETKIT_TIMEZONE` (default `Asia/Bangkok`), `STATE_FILE` (default `state.json`), `DEBUG_LOG_RAW`. Cloud-Run-only: `STATE_BUCKET` (enables the GCS state backend), `STATE_OBJECT` (default `state.json`). On Cloud Run the four required vars come from Secret Manager (`--set-secrets`); the rest are plain env vars.

## Architecture

One poll = login via `pypetkitapi` → `extract_alerts` per device → `process_alerts` reconciles each against persisted state → Telegram sends → `state.json` saved.

**Alert snapshots are emitted for every condition, active or not.** Extractors return an `Alert(is_active=True/False)` for each condition they can read — not just problems. The inactive ones are what trigger "problem cleared ✅" messages. If an extractor stops emitting an alert when the field reads clean, the cleared-message path breaks. Conditions are keyed `device_id:code` (e.g. `<id>:box_full`).

**State machine** (`AlertState` per key, persisted in `state.json`):
- Active + due → send active message, advance `alert_count`, set next `next_alert_at` via `backoff_minutes_for` (15→30→60→120, cap 120 min).
- Active but not yet due → log ETA, do nothing.
- Inactive while `state.is_active` → send cleared message, `store.reset(key)`.
- **State only advances if the Telegram send succeeds.** `_handle_active` builds a *tentative* `AlertState`, sends, and commits the tentative values only on success (`if not sent: return`). Don't refactor this into advance-then-send — a failed send must be retried next poll, not skipped.

**"แก้แล้ว" acknowledge button.** Every active alert ships an inline keyboard (`_ack_keyboard`, callback_data `ack:<key>`). Each poll, `process_telegram_updates` drains `getUpdates` (short poll, no webhook → service stays private) and sets `AlertState.acknowledged=True` for tapped keys. While `acknowledged` and still active, `_handle_active` returns early (no send, no backoff advance) — this counters PetKit's stale cloud cache that keeps reporting a problem after the user fixed it. `_handle_cleared` resets silently when `acknowledged` (no redundant "cleared ✅"), which also re-arms the key. The consumed Telegram `update_id` is persisted as `StateStore.telegram_offset` so a tap is processed once. `answerCallbackQuery` is best-effort (often stale by the next ≤15-min poll — no toast, but the tap still registers). Taps from a chat other than `TELEGRAM_CHAT_ID` are ignored. No new env var — reuses the bot token.

**State schema is now an envelope:** `{"alerts": {key: state}, "telegram_offset": N}`. `StateStore.from_dict` still reads the legacy flat `{key: state}` map (detected by the absence of an `"alerts"` sub-dict) and the first write upgrades it. Keep this back-compat — old `state.json` (git + the live GCS object) predate the envelope.

**`device_error` deduplication.** `device_error` is a catch-all that PetKit raises *alongside* a specific condition (e.g. a full Pura MAX box also reports an error code) — firing both means two messages and two backoff streams for one problem. `process_alerts` mutes `device_error` for any device that has a concrete (non-error) alert active in the same poll (`devices_with_specific_alert`); the mute path silently `store.reset`s the key so no false "error cleared" message goes out. Standalone `device_error` (no specific alert) still fires normally. Evidence for this came from mining the git history of `state.json` (co-active keys with identical timestamps), not from reading Telegram — the Bot API can't read history.

**Device dispatch** is by `type(device).__name__`: `Litter`, `Feeder`, `WaterFountain`. Each has an `_extract_*_alerts` function. `_extract_common_problem_alerts` adds `device_error` / `device_offline` to all three, plus `pet_error` for `Litter` only.

**Field reads go through `_read_attr(obj, *names)`** which handles both dicts and pydantic-model attributes and tries multiple field-name aliases. A missing field yields no alert (never a false-positive). WaterFountain fields are mostly top-level, not under `state`; battery lives in `electricity.battery_percent`.

**State migration** (`StateStore.from_dict` / `AlertState.from_dict`): legacy bare-`device_id` keys → `<id>:box_full`; legacy `was_full` → `is_active`. Keep these when changing the schema — old `state.json` files exist in git history.

`device_class` on each Alert drives the product photo (`DEVICE_PHOTOS`); `send_telegram` falls back from `sendPhoto` to `sendMessage` if Telegram rejects the photo URL. **Photos are currently disabled** (`DEVICE_PHOTOS = {}`) — alerts send as text-only because the images looked cluttered; re-add device-class→URL entries to turn them back on. `RULE_LABELS[code]` maps each code to `(emoji, active_label, cleared_label)`. `IGNORED_ERROR_CODES` (e.g. `blk_d`) suppresses non-urgent hardware errors.

### Extending

- **New alert on an existing device**: emit a new `Alert(code=...)` from that device's extractor (both active and inactive states) + add the `code` to `RULE_LABELS`.
- **New device type**: add `_extract_<type>_alerts`, dispatch it in `extract_alerts` by class name, add a `DEVICE_PHOTOS` entry, append `_extract_common_problem_alerts(...)`.

## Cloud Run wrapper

`main.py` is a thin Flask entrypoint that exposes the one-shot poll as HTTP: `GET /` (health) and `GET|POST /poll` (runs one `notifier.main_async()` cycle, returns JSON status + exit code). `Procfile` runs it under gunicorn (`web: gunicorn ... main:app`). `.gcloudignore` trims the source upload. None of this affects a local `python notifier.py` run — the wrapper is additive.

Deploy is `gcloud run deploy --source .` (Cloud Build buildpacks → Artifact Registry repo `cloud-run-source-deploy`). The service is **private** (`--no-allow-unauthenticated`); Cloud Scheduler authenticates with an OIDC token (identity = the default compute service account, granted `roles/run.invoker` on the service + `secretAccessor` on the four secrets + `storage.objectAdmin` on the state bucket). Don't make it public — `/poll` triggers PetKit logins and Telegram sends, so an open endpoint is an abuse vector. `--max-instances=1` keeps polls from racing on shared GCS state.

A Cloud Billing **budget** `petpoke-charge-alert` (10 THB, scoped to this project only) emails the billing admin at 10/50/100% of actual spend — a tripwire for leaving the free tier. Expected steady-state cost is 0. The billing account currency is **THB**; `gcloud billing budgets create` rejects a `USD` amount with a vague `INVALID_ARGUMENT`.

## State backend

Two backends, selected at runtime in `load_state`/`save_state`:
- **Local file** (default): `state.json`, used by `python notifier.py`.
- **GCS** (Cloud Run): active when `STATE_BUCKET` is set — reads/writes `gs://$STATE_BUCKET/$STATE_OBJECT` (bucket `petpoke-notifier-state`, object `state.json`). `google-cloud-storage` is imported lazily inside the GCS helpers so the local path needs no GCP deps.

`main_async` snapshots the serialized store before the poll and **skips the write entirely when nothing changed** (`State unchanged; skipping write` log line). This keeps GCS Class-A writes to a handful per day, well inside the free tier. Don't remove the change-detection guard — without it every 15-min poll writes, ~2,880/mo.

The tracked `state.json` in git is now only a **historical artifact / local-run seed** — the GHA auto-commit (`chore: update state [skip ci]`) no longer runs since the workflow is disabled. The live cross-run state lives in GCS. The frequent state commits in `git log` are pre-migration noise. When migrating runtimes, seed the new backend from the current `state.json` (e.g. `gcloud storage cp state.json gs://petpoke-notifier-state/state.json`) so already-notified active alerts don't re-fire.
