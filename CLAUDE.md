# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PetPoke polls the PetKit cloud and sends **repeating** Telegram alerts on a backoff schedule until a device problem is resolved (PetKit's own app only notifies once). Runs as a GitHub Actions cron job (`*/15` — but expect 20–45 min real spacing due to GHA cron lag). All logic lives in one file, `notifier.py`, by design — so the platform (GHA → Raspberry Pi → Cloudflare Workers) is easy to swap.

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

No test suite, linter, or build step exists. Python 3.11 (GHA pins `3.11`; uses `zoneinfo`, `from __future__ import annotations`).

Required env: `PETKIT_USERNAME`, `PETKIT_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Optional: `PETKIT_REGION` (default `TH`), `PETKIT_TIMEZONE` (default `Asia/Bangkok`), `STATE_FILE` (default `state.json`), `DEBUG_LOG_RAW`.

## Architecture

One poll = login via `pypetkitapi` → `extract_alerts` per device → `process_alerts` reconciles each against persisted state → Telegram sends → `state.json` saved.

**Alert snapshots are emitted for every condition, active or not.** Extractors return an `Alert(is_active=True/False)` for each condition they can read — not just problems. The inactive ones are what trigger "problem cleared ✅" messages. If an extractor stops emitting an alert when the field reads clean, the cleared-message path breaks. Conditions are keyed `device_id:code` (e.g. `<id>:box_full`).

**State machine** (`AlertState` per key, persisted in `state.json`):
- Active + due → send active message, advance `alert_count`, set next `next_alert_at` via `backoff_minutes_for` (15→30→60→120, cap 120 min).
- Active but not yet due → log ETA, do nothing.
- Inactive while `state.is_active` → send cleared message, `store.reset(key)`.
- **State only advances if the Telegram send succeeds.** `_handle_active` builds a *tentative* `AlertState`, sends, and commits the tentative values only on success (`if not sent: return`). Don't refactor this into advance-then-send — a failed send must be retried next poll, not skipped.

**`device_error` deduplication.** `device_error` is a catch-all that PetKit raises *alongside* a specific condition (e.g. a full Pura MAX box also reports an error code) — firing both means two messages and two backoff streams for one problem. `process_alerts` mutes `device_error` for any device that has a concrete (non-error) alert active in the same poll (`devices_with_specific_alert`); the mute path silently `store.reset`s the key so no false "error cleared" message goes out. Standalone `device_error` (no specific alert) still fires normally. Evidence for this came from mining the git history of `state.json` (co-active keys with identical timestamps), not from reading Telegram — the Bot API can't read history.

**Device dispatch** is by `type(device).__name__`: `Litter`, `Feeder`, `WaterFountain`. Each has an `_extract_*_alerts` function. `_extract_common_problem_alerts` adds `device_error` / `device_offline` to all three, plus `pet_error` for `Litter` only.

**Field reads go through `_read_attr(obj, *names)`** which handles both dicts and pydantic-model attributes and tries multiple field-name aliases. A missing field yields no alert (never a false-positive). WaterFountain fields are mostly top-level, not under `state`; battery lives in `electricity.battery_percent`.

**State migration** (`StateStore.from_dict` / `AlertState.from_dict`): legacy bare-`device_id` keys → `<id>:box_full`; legacy `was_full` → `is_active`. Keep these when changing the schema — old `state.json` files exist in git history.

`device_class` on each Alert drives the product photo (`DEVICE_PHOTOS`); `send_telegram` falls back from `sendPhoto` to `sendMessage` if Telegram rejects the photo URL. `RULE_LABELS[code]` maps each code to `(emoji, active_label, cleared_label)`. `IGNORED_ERROR_CODES` (e.g. `blk_d`) suppresses non-urgent hardware errors.

### Extending

- **New alert on an existing device**: emit a new `Alert(code=...)` from that device's extractor (both active and inactive states) + add the `code` to `RULE_LABELS`.
- **New device type**: add `_extract_<type>_alerts`, dispatch it in `extract_alerts` by class name, add a `DEVICE_PHOTOS` entry, append `_extract_common_problem_alerts(...)`.

## State commits

GHA auto-commits `state.json` after each run with `chore: update state [skip ci]` (the `[skip ci]` prevents a cron loop). The frequent state commits in `git log` are normal noise. `state.json` is tracked, not gitignored — it's the cross-run persistence layer.
