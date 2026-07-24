FROM python:3.12-slim

# ffmpeg is required by yt-dlp / spotdl for audio extraction & remuxing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first to maximise layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Persisted volume for downloads/instaloader session
RUN mkdir -p /app/downloads
VOLUME ["/app/downloads"]

# Run as non-root for safety
RUN useradd -m -u 1000 bot && chown -R bot:bot /app
USER bot

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads

CMD ["python", "main.py"]
