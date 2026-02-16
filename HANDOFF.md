# Handoff: Content Posting Lab

## Quick Setup (Agents: Start Here)

Clone the repo and run the setup script with your API keys. It handles **everything** — Python venv, pip deps, Homebrew system deps (ffmpeg, tesseract), `.env` creation, output directories, and verification.

**For AI agents / non-interactive setup** — pass keys as environment variables:

```bash
git clone https://github.com/KINGMAKER-SYSTEMS/content-posting-lab.git
cd content-posting-lab

OPENAI_API_KEY="sk-proj-..." \
XAI_API_KEY="xai-..." \
REPLICATE_API_TOKEN="r8_..." \
./setup.sh
```

Only `OPENAI_API_KEY` is required. The other keys are optional — only providers with keys will appear in the video generation UI. You can pass any combination of: `OPENAI_API_KEY`, `XAI_API_KEY`, `REPLICATE_API_TOKEN`, `FAL_KEY`, `LUMA_API_KEY`.

**For humans at a terminal** — run without env vars and it will prompt interactively:

```bash
chmod +x setup.sh && ./setup.sh
```

After setup completes, activate the venv and start the servers:

```bash
source venv/bin/activate

# Each in its own terminal:
python server.py           # localhost:8000 — video generation
python caption_server.py   # localhost:8001 — caption scraping
python burn_server.py      # localhost:8002 — caption burning
```

---

## What This Project Does

A three-server pipeline for TikTok-style video generation and captioning:

1. **Server 1** (`server.py` :8000) — Generates AI videos from text prompts using multiple providers (xAI Grok, Replicate, FAL, Luma, OpenAI Sora)
2. **Server 2** (`caption_server.py` :8001) — Scrapes TikTok profiles and extracts burned-in caption text from their videos using GPT-4o vision
3. **Server 3** (`burn_server.py` :8002) — Burns caption text onto generated videos with pixel-perfect CSS rendering → PNG overlay → ffmpeg compositing

The servers are independent. Server 3 reads from the output directories of servers 1 and 2 via the filesystem — no HTTP calls between them.

```
Server 1 → output/                    Server 2 → caption_output/
               \                           /
                → Server 3 reads both dirs
                → burn_output/{batch_id}/burned_NNN.mp4
```

---

## Environment Variables

Create a `.env` file in the project root (gitignored — you must create it yourself):

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

These must be on your PATH (the setup script installs them automatically):

| Tool | Install | Used by |
|------|---------|---------|
| `ffmpeg` / `ffprobe` | `brew install ffmpeg` | All three servers (frame extraction, video cropping, caption overlay) |
| `yt-dlp` | `pip install yt-dlp` (included in requirements.txt) | Server 2 (TikTok video listing and download) |
| `tesseract` | `brew install tesseract` | Server 2 (local OCR fallback, optional — GPT-4o is the primary method) |

## Running the Servers

Three separate terminals:

```bash
source venv/bin/activate   # in each terminal

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

For the burn server specifically:
1. Start server 3: `python burn_server.py`
2. Open `http://localhost:8002`
3. Select a video folder from the dropdown, pick a caption source, click "Align & Preview"
4. Hit "Burn All" — should produce MP4s in `burn_output/`
5. Download as ZIP from the sidebar

## Architecture Overview

Read `CLAUDE.md` for the full breakdown, but the short version:

- **Server 1** generates AI videos → writes to `output/`
- **Server 2** scrapes TikTok captions → writes to `caption_output/`
- **Server 3** burns captions onto videos → reads from both dirs, writes to `burn_output/`

No database. No inter-server communication. Just filesystem.
