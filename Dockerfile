FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_FILE=/data/state.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY notifier.py runner.py ./

# Cross-run alert state persists in a mounted volume.
VOLUME ["/data"]

CMD ["python", "runner.py"]
