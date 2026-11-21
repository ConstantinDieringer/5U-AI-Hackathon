# Runs the whole forwarder (poller + agents + UI) in one container.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Voice offers are transcribed locally (the gateway refuses audio): bake the Whisper model in,
# so no container start depends on the model hub.
ENV SPEECH_MODEL=base.en SPEECH_MODEL_DIR=/opt/whisper
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8', download_root='/opt/whisper')"
COPY . .
# SQLite and attachments live on a mounted volume (see railway.json / DEPLOY.md)
ENV DB_PATH=/data/forwarder.db INBOX_DIR=/data/inbox
CMD ["python", "run.py"]
