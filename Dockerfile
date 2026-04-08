# Stage 1: Build frontend
FROM node:20-bookworm-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ .
# Cache-bust: change this comment to force frontend rebuild → v2
ENV NODE_ENV=production
RUN npm run build

# Stage 2: Backend
FROM python:3.10-slim-bookworm
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN grep -v -E '^(playwright|pytesseract|tiktokautouploader)' requirements.txt > requirements-prod.txt \
    && pip install --no-cache-dir -r requirements-prod.txt

COPY app.py main.py project_manager.py telegram_bot.py debug_logger.py ./
COPY routers/ ./routers/
COPY providers/ ./providers/
COPY scraper/ ./scraper/
COPY services/ ./services/
COPY fonts/ ./fonts/
COPY --from=frontend /app/frontend/dist ./frontend/dist

RUN mkdir -p output caption_output burn_output projects

EXPOSE 8000
CMD ["python", "main.py"]
