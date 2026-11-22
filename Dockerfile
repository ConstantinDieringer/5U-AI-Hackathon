# Runs the whole forwarder (poller + agents + UI) in one container.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV SPEECH_MODEL=base SPEECH_MODEL_DIR=/opt/whisper
# Keep the model layer independent of application, tariff and documentation edits.
RUN python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['SPEECH_MODEL'], device='cpu', compute_type='int8', download_root=os.environ['SPEECH_MODEL_DIR'])"
COPY app/ app/
COPY data/ data/
COPY run.py .
# SQLite and attachments live on a mounted volume (see railway.json / DEPLOY.md)
ENV DB_PATH=/data/forwarder.db INBOX_DIR=/data/inbox
CMD ["python", "run.py"]
