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

# System deps: ffmpeg only (yt-dlp handles video listing via CLI, no browser needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Python deps (skip playwright/tesseract — not needed in production)
COPY requirements.txt .
RUN grep -v -E '^(playwright|pytesseract)' requirements.txt > requirements-prod.txt \
    && pip install --no-cache-dir -r requirements-prod.txt

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
CMD ["sh", "-c", "echo 'PORT=${PORT}' && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level info --timeout-keep-alive 30"]
