"""
HTTP entrypoint for Cloud Run.

PetPoke's core logic (notifier.py) is a one-shot poll. Cloud Run is a
request-driven service, so this thin Flask wrapper exposes the poll as an
HTTP endpoint that Cloud Scheduler hits on a precise cron schedule —
replacing the GitHub Actions cron (which had 15–30 min lag).

Endpoints:
    GET  /        health check
    POST /poll    run one poll cycle (also accepts GET for easy manual testing)

Run locally:  gunicorn -b :8080 main:app
"""

from __future__ import annotations

import asyncio

from flask import Flask, jsonify

from notifier import main_async

app = Flask(__name__)


@app.get("/")
def health() -> tuple[str, int]:
    return "petpoke ok", 200


@app.route("/poll", methods=["GET", "POST"])
def poll():
    code = asyncio.run(main_async())
    ok = code == 0
    return jsonify(status="ok" if ok else "error", exit_code=code), (200 if ok else 500)
