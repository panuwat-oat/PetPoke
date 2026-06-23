# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PetPoke polls the PetKit cloud and sends **repeating** Telegram alerts on a backoff schedule until a device problem is resolved (PetKit's own app only notifies once). Core poll logic lives in one file, `notifier.py`, by design — so the runtime platform is easy to swap.

**Runtime: personal VPS via Docker** (Contabo, host `personal-vps`/`contabo` in `~/.ssh/config` → `147.93.156.210`, root; project lives in `/opt/petpoke`). A single long-lived container runs `runner.py`, which calls `notifier.main_async()` on an internal loop (`POLL_INTERVAL_MINUTES` = **5 min** in compose on the VPS; `runner.py`'s own default is 15) — no external scheduler. The cadence was tightened from 15→5 once off Cloud Run, since the VPS has no free-tier quota to respect. `docker-compose.yml` sets `restart: unless-stopped` (survives reboot) and mounts `./data` for state. Lineage: GitHub Actions cron (drift 20–45 min) → Google Cloud Run + Cloud Scheduler → **now VPS Docker** (Cloud Run + Scheduler + GCS bucket + secrets + Artifact Registry repo all torn down). `notifier.py` stays platform-agnostic so it can still run anywhere (local, Pi, etc.).

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
# VPS — redeploy after code changes (copy runtime files, then rebuild)
tar czf - Dockerfile docker-compose.yml requirements.txt notifier.py runner.py .dockerignore \
  | ssh personal-vps 'tar xzf - -C /opt/petpoke && rm -f /opt/petpoke/._*'
ssh personal-vps 'cd /opt/petpoke && docker compose up -d --build'

# Tail logs
ssh personal-vps 'docker logs -f petpoke'

# Manually fire one poll (one-shot inside the container, separate from the loop)
ssh personal-vps 'docker exec petpoke python -c "import asyncio,notifier; raise SystemExit(asyncio.run(notifier.main_async()))"'

# Rotate a secret value: edit /opt/petpoke/.env then restart
ssh personal-vps 'cd /opt/petpoke && docker compose restart'
```

Required env: `PETKIT_USERNAME`, `PETKIT_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Optional: `PETKIT_REGION` (default `TH`), `PETKIT_TIMEZONE` (default `Asia/Bangkok`), `STATE_FILE` (default `state.json`; set to `/data/state.json` in the container), `POLL_INTERVAL_MINUTES` (code default 15, set to 5 on the VPS; used by `runner.py` only), `DEBUG_LOG_RAW`. On the VPS the four required secrets live in `/opt/petpoke/.env` (mode 600, `env_file` in compose); the non-secret vars are set in `docker-compose.yml`. The `STATE_BUCKET`/`STATE_OBJECT` GCS backend still exists in `notifier.py` (lazy import) but is unused now — leave it for portability.

## Architecture

One poll = login via `pypetkitapi` → `extract_alerts` per device → `process_alerts` reconciles each against persisted state → Telegram sends → `state.json` saved.

**Alert snapshots are emitted for every condition, active or not.** Extractors return an `Alert(is_active=True/False)` for each condition they can read — not just problems. The inactive ones are what trigger "problem cleared ✅" messages. If an extractor stops emitting an alert when the field reads clean, the cleared-message path breaks. Conditions are keyed `device_id:code` (e.g. `<id>:box_full`).

**State machine** (`AlertState` per key, persisted in `state.json`):
- Active + due → send active message, advance `alert_count`, set next `next_alert_at` via `backoff_minutes_for` (15→30→60→120, cap 120 min).
- Active but not yet due → log ETA, do nothing.
- Inactive while `state.is_active` → send cleared message, `store.reset(key)`.
- **State only advances if the Telegram send succeeds.** `_handle_active` builds a *tentative* `AlertState`, sends, and commits the tentative values only on success (`if not sent: return`). Don't refactor this into advance-then-send — a failed send must be retried next poll, not skipped.

**"แก้แล้ว" acknowledge button.** Every active alert ships an inline keyboard (`_ack_keyboard`, callback_data `ack:<key>`). Each poll, `process_telegram_updates` drains `getUpdates` (short poll, no webhook → service stays private) and sets `AlertState.acknowledged=True` for tapped keys. While `acknowledged` and still active, `_handle_active` returns early (no send, no backoff advance) — this counters PetKit's stale cloud cache that keeps reporting a problem after the user fixed it. `_handle_cleared` resets silently when `acknowledged` (no redundant "cleared ✅"), which also re-arms the key. The consumed Telegram `update_id` is persisted as `StateStore.telegram_offset` so a tap is processed once. `answerCallbackQuery` is best-effort (often stale by the next ≤15-min poll — no toast, but the tap still registers). Taps from a chat other than `TELEGRAM_CHAT_ID` are ignored. No new env var — reuses the bot token.

**State schema is now an envelope:** `{"alerts": {key: state}, "telegram_offset": N}`. `StateStore.from_dict` still reads the legacy flat `{key: state}` map (detected by the absence of an `"alerts"` sub-dict) and the first write upgrades it. Keep this back-compat — old `state.json` (git + the live VPS state file, originally seeded from GCS) predate the envelope.

**`device_error` deduplication.** `device_error` is a catch-all that PetKit raises *alongside* a specific condition (e.g. a full Pura MAX box also reports an error code) — firing both means two messages and two backoff streams for one problem. `process_alerts` mutes `device_error` for any device that has a concrete (non-error) alert active in the same poll (`devices_with_specific_alert`); the mute path silently `store.reset`s the key so no false "error cleared" message goes out. Standalone `device_error` (no specific alert) still fires normally. Evidence for this came from mining the git history of `state.json` (co-active keys with identical timestamps), not from reading Telegram — the Bot API can't read history.

**Device dispatch** is by `type(device).__name__`: `Litter`, `Feeder`, `WaterFountain`. Each has an `_extract_*_alerts` function. `_extract_common_problem_alerts` adds `device_error` / `device_offline` to all three, plus `pet_error` for `Litter` only.

**Field reads go through `_read_attr(obj, *names)`** which handles both dicts and pydantic-model attributes and tries multiple field-name aliases. A missing field yields no alert (never a false-positive). WaterFountain fields are mostly top-level, not under `state`; battery lives in `electricity.battery_percent`.

**State migration** (`StateStore.from_dict` / `AlertState.from_dict`): legacy bare-`device_id` keys → `<id>:box_full`; legacy `was_full` → `is_active`. Keep these when changing the schema — old `state.json` files exist in git history.

`device_class` on each Alert drives the product photo (`DEVICE_PHOTOS`); `send_telegram` falls back from `sendPhoto` to `sendMessage` if Telegram rejects the photo URL. **Photos are currently disabled** (`DEVICE_PHOTOS = {}`) — alerts send as text-only because the images looked cluttered; re-add device-class→URL entries to turn them back on. `RULE_LABELS[code]` maps each code to `(emoji, active_label, cleared_label)`. `IGNORED_ERROR_CODES` (e.g. `blk_d`) suppresses non-urgent hardware errors.

### Extending

- **New alert on an existing device**: emit a new `Alert(code=...)` from that device's extractor (both active and inactive states) + add the `code` to `RULE_LABELS`.
- **New device type**: add `_extract_<type>_alerts`, dispatch it in `extract_alerts` by class name, add a `DEVICE_PHOTOS` entry, append `_extract_common_problem_alerts(...)`.

## VPS / Docker deployment

`runner.py` is the long-lived entrypoint: an internal loop that calls `notifier.main_async()` every `POLL_INTERVAL_MINUTES` (5 on the VPS, code default 15), subtracting elapsed poll time from the sleep so cadence doesn't drift cumulatively (not wall-clock aligned like cron — acceptable for minute-scale backoff). It's additive, exactly like `main.py` (the old Cloud Run Flask wrapper, now deleted) was — `notifier.py` is untouched. `Dockerfile` is `python:3.11-slim` + `requirements.txt` + `notifier.py`/`runner.py`, `CMD python runner.py`. `docker-compose.yml` wires `env_file: .env` (4 secrets), non-secret env (region/tz/interval/`STATE_FILE=/data/state.json`), the `./data:/data` state volume, and `restart: unless-stopped`.

No HTTP endpoint is exposed (unlike Cloud Run's `/poll`) — the loop is self-contained, so there's no public-abuse surface. The Telegram "แก้แล้ว" button still works via `getUpdates` short-poll (no webhook needed). The VPS also runs an unrelated `byd-bot` container; `--max-instances`-style racing isn't a concern (single container, single state file).

**Logging.** App logs go to stdout (`PYTHONUNBUFFERED=1`, format `ts LEVEL logger: msg`); `runner.py` catches per-poll exceptions and logs `LOG.exception` so a bad cycle never kills the loop. Compose caps the `json-file` driver at `max-size 10m` × `max-file 5` (50MB) — the driver is unbounded by default and 5-min polling would otherwise grow it forever. Timestamps are Bangkok local: the image installs **system `tzdata`** (slim ships none; the `tzdata` pip package only covers Python's `zoneinfo`, not libc) and compose sets `TZ=Asia/Bangkok`. The VPS host timezone is also `Asia/Bangkok` (`timedatectl set-timezone`).

**Deploy = copy runtime files + `docker compose up -d --build`** (see Commands). Don't copy `CLAUDE.md`/`.claude` to the server (global rule); the `tar` file-list and `.dockerignore` both exclude them. macOS `tar` emits AppleDouble `._*` files — strip them on the server after extract (`rm -f /opt/petpoke/._*`).

GCP teardown is **complete**: Cloud Run service `petpoke`, Cloud Scheduler job `petpoke-poll`, GCS bucket `petpoke-notifier-state`, Cloud Build source bucket, Artifact Registry repo `cloud-run-source-deploy`, and the 4 Secret Manager secrets are all deleted. Only the (now-empty) project `petpoke-notifier` and its billing budget `petpoke-charge-alert` remain.

## State backend

Two backends, selected at runtime in `load_state`/`save_state`:
- **Local file** (default + current VPS runtime): `state.json` / `STATE_FILE`. On the VPS this is `/data/state.json`, bind-mounted from `/opt/petpoke/data` so it survives container rebuilds.
- **GCS** (legacy, unused): active when `STATE_BUCKET` is set — `google-cloud-storage` is imported lazily so the local/VPS path needs no GCP deps. Kept for runtime portability even though Cloud Run is gone.

`main_async` snapshots the serialized store before the poll and **skips the write entirely when nothing changed** (`State unchanged; skipping write` log line). Don't remove the change-detection guard (cheap, avoids needless writes ~2,880/mo).

The tracked `state.json` in git is only a **historical artifact / local-run seed**. The live cross-run state now lives in `/opt/petpoke/data/state.json` on the VPS (seeded from the old GCS object during migration). The frequent state commits in `git log` are pre-migration noise. When migrating runtimes, seed the new backend from the current state (e.g. `scp state.json personal-vps:/opt/petpoke/data/state.json`) so already-notified active alerts don't re-fire.
