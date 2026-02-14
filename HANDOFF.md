# Handoff: Content Posting Lab

## Getting Started

```bash
git clone https://github.com/KINGMAKER-SYSTEMS/content-posting-lab.git
cd content-posting-lab
pip install -r requirements.txt
brew install ffmpeg tesseract       # macOS system deps
playwright install chromium         # only if you need browser-based TikTok scraping
```

## Environment Variables

Create a `.env` file in the project root. This file is gitignored — you need to create it yourself.

```env
# ── Required ──────────────────────────────────────────────

# OpenAI — used for GPT-4o vision caption extraction (server 2)
# AND for Sora 2 video generation (server 1). You need this one.
OPENAI_API_KEY=sk-proj-...

# ── Video Generation Providers (server 1) ─────────────────
# Only providers with keys configured will show up in the UI.
# You don't need all of them — just set whichever ones you have access to.

# xAI Grok Imagine Video
XAI_API_KEY=xai-...

# Replicate (gives you MiniMax Hailuo, Wan 2.1, Kling v2.1)
REPLICATE_API_TOKEN=r8_...

# FAL.ai (gives you Wan 2.5, Kling 2.5, Ovi)
FAL_KEY=...

# Luma Dream Machine (Ray 2)
LUMA_API_KEY=...
```

### Where to get keys

| Key | Sign up | Notes |
|-----|---------|-------|
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys | Needed for caption extraction (GPT-4o vision). Also powers Sora 2 video gen. |
| `XAI_API_KEY` | https://console.x.ai/ | Grok Imagine Video. Fast, decent quality. |
| `REPLICATE_API_TOKEN` | https://replicate.com/account/api-tokens | Pay-per-use. Gets you access to MiniMax, Wan, and Kling models. |
| `FAL_KEY` | https://fal.ai/dashboard/keys | Similar to Replicate. Has newer model versions (Wan 2.5, Kling 2.5). |
| `LUMA_API_KEY` | https://lumalabs.ai/dream-machine/api | Luma Dream Machine / Ray 2. |

### What's strictly required vs optional

- **`OPENAI_API_KEY`** — Required if you're using the caption scrape server (server 2) at all, since it uses GPT-4o to read captions from video frames.
- **Everything else** — Optional. The video generation server dynamically shows only providers that have keys configured. You can run with just one provider.

## System Dependencies

These must be on your PATH:

| Tool | Install | Used by |
|------|---------|---------|
| `ffmpeg` / `ffprobe` | `brew install ffmpeg` | All three servers (frame extraction, video cropping, caption overlay) |
| `yt-dlp` | `pip install yt-dlp` (included in requirements.txt) | Server 2 (TikTok video listing and download) |
| `tesseract` | `brew install tesseract` | Server 2 (local OCR fallback, optional — GPT-4o is the primary method) |

## Running the Servers

Three separate terminals:

```bash
python server.py           # localhost:8000 — video generation
python caption_server.py   # localhost:8001 — caption scraping
python burn_server.py      # localhost:8002 — caption burning
```

You don't need all three running at once. They're independent — only server 3 (burn) reads from the other two's output directories, and it does that via the filesystem, not HTTP.

## TikTok Authentication (Server 2)

If you're using the caption scraper, you may need a TikTok session for yt-dlp to work reliably:

```bash
python login_tiktok.py
```

This opens a browser window — log into TikTok manually, then close it. Session gets saved to `tiktok_auth.json` (gitignored). If scraping starts getting rate-limited or failing, re-run this.

## Quick Smoke Test

1. Start server 1: `python server.py`
2. Open `http://localhost:8000`
3. Pick any provider you have a key for, type a prompt, hit generate
4. If you get a video back, you're good

## Architecture Overview

Read `CLAUDE.md` for the full breakdown, but the short version:

- **Server 1** generates AI videos → writes to `output/`
- **Server 2** scrapes TikTok captions → writes to `caption_output/`
- **Server 3** burns captions onto videos → reads from both dirs, writes to `burn_output/`

No database. No inter-server communication. Just filesystem.
