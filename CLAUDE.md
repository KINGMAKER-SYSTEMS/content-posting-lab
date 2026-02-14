# Content Posting Lab

TikTok-style video generation and captioning pipeline. Three **separate** FastAPI servers that will be unified later but are independent during dev.

**Do not combine the servers.** They are intentionally separate right now.

## The Three Servers

### 1. Video Generation Server — `server.py` (port 8000)

Generates AI videos from text prompts (and optional image input) using multiple providers.

- **What it does:** Takes a prompt, fans it out to one or more AI video APIs, polls until done, downloads the MP4s, optionally crops aspect ratio.
- **UI:** `static/index.html`
- **Output:** `output/{provider}/{prompt_slug}/{job_id}_{index}.mp4`
- **State:** In-memory only (jobs dict). Nothing persists across restarts.
- **Key endpoints:**
  - `POST /api/generate` — submit a generation job (prompt, provider, count, duration, aspect_ratio, resolution, optional media upload)
  - `GET /api/jobs/{job_id}` — poll job status
  - `GET /api/providers` — list which providers have API keys configured
  - `GET /api/jobs/{job_id}/download-all` — ZIP of all completed videos
- **Providers:** Grok (xAI), Replicate (MiniMax/Wan/Kling), FAL (Wan/Kling/Ovi), Luma Dream Machine, OpenAI Sora 2
- Each provider has its own async flow. Some return a URL to poll, some return the file directly. Aspect ratio support varies — some providers force landscape and the server auto-crops to 9:16 via ffmpeg.

### 2. Caption Scrape Server — `caption_server.py` (port 8001)

Scrapes TikTok profiles to extract the burned-in caption text from their videos.

- **What it does:** Given a TikTok profile URL, lists their videos (via yt-dlp), downloads each one, extracts a frame at the 2-second mark (ffmpeg), then sends that frame to GPT-4o vision to read the burned-in caption text.
- **UI:** `static/captions/index.html`
- **Output:** `caption_output/{username}/` — contains `frames/`, `videos/` (temporary), and `captions.csv`
- **Key endpoints:**
  - `WebSocket /ws/{job_id}` — real-time progress. Client sends `{"action": "start", "profile_url": "...", "max_videos": 20, "sort": "latest"}`.
  - `GET /api/export/{username}` — download the CSV
- **Pipeline:** URL collection (yt-dlp) → download + frame extract (batches of 5) → GPT-4o OCR (batches of 10) → write CSV
- Videos are deleted after frame extraction to save disk. The CSV columns are `video_id, video_url, caption, error`. The GPT-4o prompt specifically ignores TikTok UI (usernames, likes, sounds) and only extracts the actual burned-in caption overlay.

### 3. Caption Burn Server — `burn_server.py` (port 8002)

Burns caption text onto generated videos — this is how we print captions onto our AI-generated content.

- **What it does:** Takes video-caption pairs, renders the text as a transparent PNG overlay (Pillow), then composites it onto the video (ffmpeg). Outputs a new MP4.
- **UI:** `static/burn/index.html`
- **Output:** `burn_output/{batch_id}/burned_NNN.mp4`
- **Key endpoints:**
  - `GET /api/videos` — lists videos from `output/` (the video gen server's output)
  - `GET /api/captions` — lists caption CSVs from `caption_output/` (the scrape server's output)
  - `GET /api/burned` — lists completed burn batches
  - `WebSocket /ws/burn` — send `{"pairs": [...], "position": "top|center|bottom", "fontSize": 58}` to start burning
- This server reads from both other servers' output directories. Font is `fonts/TikTokSans16pt-Bold.ttf`. Text is white with 4px black stroke. Positioning: top (8% from top), center, bottom (8% from bottom).

## How They Connect

```
Server 1 (video gen)          Server 2 (caption scrape)
output/{provider}/...         caption_output/{username}/captions.csv
        \                           /
         \                         /
          v                       v
       Server 3 (burn) reads both directories
       burn_output/{batch_id}/burned_NNN.mp4
```

The servers don't call each other over HTTP. Server 3 reads the filesystem directories that servers 1 and 2 write to.

## Starting the Servers

Each one in its own terminal:

```bash
python server.py           # port 8000 — video generation
python caption_server.py   # port 8001 — caption scraping
python burn_server.py      # port 8002 — caption burning
```

## Prerequisites

```bash
pip install -r requirements.txt
playwright install chromium          # only if using Playwright scraping mode
brew install ffmpeg tesseract        # system deps (macOS)
```

API keys in `.env` — see HANDOFF.md for details on what each key does.

## Shared Code — `scraper/`

Used by the caption server (server 2):

| File | Purpose |
|------|---------|
| `frame_extractor.py` | yt-dlp video listing/download, ffmpeg frame extraction |
| `caption_extractor.py` | GPT-4o vision API call to read captions from a frame |
| `ocr_extractor.py` | Local Tesseract OCR fallback (crops center 60%, binarizes) |
| `tiktok_scraper.py` | Playwright browser scraping (alternative to yt-dlp, anti-detection) |

## Directory Layout

```
content-posting-lab/
├── server.py                  # Server 1: video generation (port 8000)
├── caption_server.py          # Server 2: caption scraping (port 8001)
├── burn_server.py             # Server 3: caption burning  (port 8002)
├── scraper/                   # Shared extraction utilities (used by server 2)
├── static/
│   ├── index.html             # UI for server 1
│   ├── captions/index.html    # UI for server 2
│   └── burn/index.html        # UI for server 3
├── fonts/                     # TikTokSans + Montserrat font files
├── output/                    # Server 1 writes here (gitignored)
├── caption_output/            # Server 2 writes here (gitignored)
├── burn_output/               # Server 3 writes here (gitignored)
├── .env                       # API keys (gitignored)
├── tiktok_auth.json           # Saved TikTok browser session (gitignored)
└── requirements.txt           # Python deps
```

## Conventions and Gotchas

- **All three servers use FastAPI + uvicorn.** Async throughout.
- **WebSockets for real-time progress** in servers 2 and 3. Server 1 uses polling (`GET /api/jobs/{id}`).
- **ffmpeg and ffprobe must be on PATH.** Used by all three servers.
- **yt-dlp must be on PATH.** Used by server 2 for TikTok video listing and downloading.
- **Job state is in-memory only.** Restart = lose tracking (output files on disk survive).
- **No database.** Everything is filesystem-based. CSVs for caption data.
- **No inter-server HTTP calls.** Server 3 reads server 1 and 2's output dirs directly.
- **TikTok auth** saved in `tiktok_auth.json`. Run `python login_tiktok.py` to refresh if scraping fails.
- **Videos are deleted after frame extraction** in server 2 to save disk.
- **Burn server processes sequentially** (one video at a time) due to ffmpeg resource usage.

## Dev Workflow

1. **Generate videos** — server 1 UI at `localhost:8000`. Pick provider, write prompt, generate.
2. **Scrape captions** — server 2 UI at `localhost:8001`. Paste TikTok profile URL, scrape and extract captions.
3. **Burn captions onto videos** — server 3 UI at `localhost:8002`. Pair videos with captions, burn.
4. Result: `burn_output/{batch_id}/` has final captioned videos ready to post.
