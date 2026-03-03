# ── Stage 1: Build frontend ──────────────────────────────────
FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Production runtime ─────────────────────────────
FROM python:3.12-slim

# System dependencies: ffmpeg, tesseract, Playwright Chromium deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

# App source code
COPY . .

# Overwrite with clean frontend build from stage 1
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Railway injects PORT at runtime
EXPOSE 8000
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
