# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies (Only what pip/ffmpeg needs)
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    curl \
    ca-certificates \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install static FFmpeg and FFprobe binaries (yt-dlp needs these)
RUN mkdir -p /opt/ffmpeg \
    && curl -L https://johnvansickle.com | tar -xJ -C /opt/ffmpeg --strip-components=1 \
    && ln -s /opt/ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && ln -s /opt/ffmpeg/ffprobe /usr/local/bin/ffprobe
# Install Deno runtime securely for yt-dlp-ejs
RUN curl -fsSL https://deno.land | sh


# Copy dependency manifests first for layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy application
COPY . .

# Create runtime directories
RUN mkdir -p uploads outputs temp templates static

# Runtime verification
RUN python - <<'PY'
import shutil

required = ["ffmpeg", "ffprobe", "yt-dlp", "deno"]

missing = [x for x in required if not shutil.which(x)]

if missing:
    raise RuntimeError(f"Missing binaries: {missing}")

print("All runtime binaries available. Deno successfully mapped for yt-dlp.")
PY

EXPOSE 10000

# Single-process uvicorn only
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
