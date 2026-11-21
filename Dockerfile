# Runs the whole forwarder (poller + agents + UI) in one container.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Preload the offline fallback; runtime never waits for a model download.
ENV HF_HOME=/opt/whisper-cache
RUN python -m app.mail.audio_local base --download
# SQLite and attachments live on a mounted volume (see railway.json / DEPLOY.md)
ENV DB_PATH=/data/forwarder.db INBOX_DIR=/data/inbox
CMD ["python", "run.py"]
