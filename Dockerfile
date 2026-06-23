FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_FILE=/data/state.json

WORKDIR /app

# System tzdata so libc localtime (and thus Python log timestamps) honors the
# TZ env var. The slim base ships without zoneinfo; the tzdata pip package only
# covers Python's zoneinfo module, not libc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY notifier.py runner.py ./

# Cross-run alert state persists in a mounted volume.
VOLUME ["/data"]

CMD ["python", "runner.py"]
