FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEEPFACE_HOME=/models TF_CPP_MIN_LOG_LEVEL=3
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# Download face-model weights at build time so the first guest isn't slow
RUN mkdir -p /models && DATA_DIR=/tmp/warm python -c "from app import faces; faces.warm_up()"

EXPOSE 8000
# Keep ONE worker: the photo indexer runs inside the web process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
