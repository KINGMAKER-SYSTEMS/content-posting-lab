# Stage 1: Build frontend
FROM node:20-bookworm-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ .
ENV NODE_ENV=production
RUN npm run build

# Stage 2: Backend with system deps and app
FROM python:3.10-slim-bookworm
WORKDIR /app

# System deps: ffmpeg, tesseract (optional OCR), and deps for Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Python deps (includes yt-dlp, playwright)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium (for caption scraping); non-fatal if download fails
RUN playwright install chromium || true

# App code
COPY app.py main.py project_manager.py ./
COPY routers/ ./routers/
COPY providers/ ./providers/
COPY scraper/ ./scraper/

# Static assets and built frontend
COPY fonts/ ./fonts/
COPY --from=frontend /app/frontend/dist ./frontend/dist

# Optional dirs the app creates at runtime (ensure they exist)
RUN mkdir -p output caption_output burn_output projects

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
