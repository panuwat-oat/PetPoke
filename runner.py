"""
Long-running loop driver for the VPS / Docker deployment.

PetPoke's core logic (notifier.py) is a one-shot poll. On Cloud Run, Cloud
Scheduler drove the cadence by hitting an HTTP endpoint. On a plain VPS there
is no external scheduler, so this wrapper calls the same one-shot poll on a
fixed interval inside a single long-lived container (`restart: unless-stopped`).

notifier.py stays platform-agnostic — this file is additive, exactly like
main.py was for Cloud Run.

Interval via POLL_INTERVAL_MINUTES (default 15, matching the old scheduler).
Elapsed poll time is subtracted from the sleep so the cadence does not drift
cumulatively, though it is not wall-clock aligned the way cron is.

Run locally:  POLL_INTERVAL_MINUTES=15 python runner.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from notifier import main_async

LOG = logging.getLogger("petpoke.runner")

_MIN_INTERVAL_SECONDS = 60.0


async def _loop() -> None:
    interval_min = float(os.environ.get("POLL_INTERVAL_MINUTES", "15"))
    interval_s = max(_MIN_INTERVAL_SECONDS, interval_min * 60.0)
    LOG.info("PetPoke runner started; polling every %.0f min", interval_s / 60.0)

    while True:
        start = time.monotonic()
        try:
            code = await main_async()
            LOG.info("poll cycle done (exit_code=%s)", code)
        except Exception:
            LOG.exception("poll cycle crashed; continuing to next cycle")
        elapsed = time.monotonic() - start
        await asyncio.sleep(max(1.0, interval_s - elapsed))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_loop())
