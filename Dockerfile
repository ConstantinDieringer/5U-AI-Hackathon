# Runs the whole forwarder (poller + agents + UI) in one container.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# SQLite and attachments live on a mounted volume (see railway.json / DEPLOY.md)
ENV DB_PATH=/data/forwarder.db INBOX_DIR=/data/inbox
CMD ["python", "run.py"]
