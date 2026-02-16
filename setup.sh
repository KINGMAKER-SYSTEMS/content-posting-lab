#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────
# Content Posting Lab — One-shot setup script
#
# Usage (interactive — human at terminal):
#   chmod +x setup.sh && ./setup.sh
#
# Usage (non-interactive — AI agent or CI):
#   OPENAI_API_KEY=sk-proj-... XAI_API_KEY=xai-... ./setup.sh
#
#   Pass any API keys as environment variables. The script
#   will write them to .env automatically without prompting.
#   Keys not provided are skipped (you can add them later).
#
# What it does:
#   1. Creates a Python virtual environment (venv/)
#   2. Installs Python dependencies from requirements.txt
#   3. Installs system dependencies (ffmpeg, tesseract) via Homebrew
#   4. Creates .env with your API keys
#   5. Creates the output directories the servers expect
#   6. Verifies everything is ready to go
# ───────────────────────────────────────────────────────────

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}ℹ${NC}  $1"; }
ok()    { echo -e "${GREEN}✓${NC}  $1"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $1"; }
fail()  { echo -e "${RED}✗${NC}  $1"; }
header(){ echo -e "\n${BOLD}── $1 ──${NC}"; }

# Detect non-interactive mode (keys passed as env vars)
# If ANY key env var is set, we skip the interactive prompts
NONINTERACTIVE=false
if [[ -n "${OPENAI_API_KEY:-}" ]] || [[ -n "${XAI_API_KEY:-}" ]] || \
   [[ -n "${REPLICATE_API_TOKEN:-}" ]] || [[ -n "${FAL_KEY:-}" ]] || \
   [[ -n "${LUMA_API_KEY:-}" ]]; then
    NONINTERACTIVE=true
    info "Non-interactive mode: API keys detected from environment."
fi

# ── Sanity checks ──────────────────────────────────────────

if [[ ! -f "requirements.txt" ]]; then
    fail "requirements.txt not found. Run this script from the project root."
    exit 1
fi

if [[ ! -f "burn_server.py" ]]; then
    fail "burn_server.py not found. Are you in the content-posting-lab directory?"
    exit 1
fi

# ── 1. Python virtual environment ──────────────────────────

header "Python Environment"

if [[ -d "venv" ]]; then
    ok "Virtual environment already exists (venv/)"
else
    info "Creating virtual environment..."
    python3 -m venv venv
    ok "Created virtual environment (venv/)"
fi

info "Activating virtual environment..."
source venv/bin/activate
ok "Using Python: $(python --version) at $(which python)"

# ── 2. Python dependencies ─────────────────────────────────

header "Python Dependencies"

info "Installing from requirements.txt..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
ok "All Python packages installed"

# ── 3. System dependencies (Homebrew) ──────────────────────

header "System Dependencies"

# Check for Homebrew
if ! command -v brew &> /dev/null; then
    warn "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    ok "Homebrew installed"
else
    ok "Homebrew found"
fi

# ffmpeg
if command -v ffmpeg &> /dev/null; then
    ok "ffmpeg already installed ($(ffmpeg -version 2>&1 | head -1 | awk '{print $3}'))"
else
    info "Installing ffmpeg (required by all 3 servers)..."
    brew install ffmpeg
    ok "ffmpeg installed"
fi

# ffprobe (comes with ffmpeg, but verify)
if command -v ffprobe &> /dev/null; then
    ok "ffprobe available"
else
    fail "ffprobe not found even though ffmpeg is installed. Something is wrong."
    exit 1
fi

# tesseract (optional — OCR fallback for server 2)
if command -v tesseract &> /dev/null; then
    ok "tesseract already installed"
else
    info "Installing tesseract (optional OCR fallback for caption server)..."
    brew install tesseract
    ok "tesseract installed"
fi

# yt-dlp (installed via pip, but verify it's on PATH)
if command -v yt-dlp &> /dev/null; then
    ok "yt-dlp available on PATH"
else
    warn "yt-dlp not found on PATH. It was installed via pip but may need venv activation."
fi

# ── 4. .env file ──────────────────────────────────────────

header "API Keys (.env)"

write_env_file() {
    # Takes key values as arguments (empty string = skip)
    local openai_key="${1:-}"
    local xai_key="${2:-}"
    local replicate_key="${3:-}"
    local fal_key="${4:-}"
    local luma_key="${5:-}"

    cat > .env << 'ENVHEADER'
# ── Required ──────────────────────────────────────────────

# OpenAI — used for GPT-4o vision caption extraction (server 2)
# AND for Sora 2 video generation (server 1). You need this one.
ENVHEADER

    if [[ -n "$openai_key" ]]; then
        echo "OPENAI_API_KEY=$openai_key" >> .env
    else
        echo "# OPENAI_API_KEY=" >> .env
    fi

    cat >> .env << 'ENVMID'

# ── Video Generation Providers (server 1) ─────────────────
# Only providers with keys configured will show up in the UI.

# xAI Grok Imagine Video
ENVMID

    if [[ -n "$xai_key" ]]; then
        echo "XAI_API_KEY=$xai_key" >> .env
    else
        echo "# XAI_API_KEY=" >> .env
    fi

    echo "" >> .env
    echo "# Replicate (gives you MiniMax Hailuo, Wan 2.1, Kling v2.1)" >> .env
    if [[ -n "$replicate_key" ]]; then
        echo "REPLICATE_API_TOKEN=$replicate_key" >> .env
    else
        echo "# REPLICATE_API_TOKEN=" >> .env
    fi

    echo "" >> .env
    echo "# FAL.ai (gives you Wan 2.5, Kling 2.5, Ovi)" >> .env
    if [[ -n "$fal_key" ]]; then
        echo "FAL_KEY=$fal_key" >> .env
    else
        echo "# FAL_KEY=" >> .env
    fi

    echo "" >> .env
    echo "# Luma Dream Machine (Ray 2)" >> .env
    if [[ -n "$luma_key" ]]; then
        echo "LUMA_API_KEY=$luma_key" >> .env
    else
        echo "# LUMA_API_KEY=" >> .env
    fi
}

if [[ -f ".env" ]]; then
    warn ".env already exists. Skipping key setup."
    info "To reconfigure, delete .env and re-run this script."
elif [[ "$NONINTERACTIVE" == true ]]; then
    # Non-interactive: use env vars directly
    write_env_file \
        "${OPENAI_API_KEY:-}" \
        "${XAI_API_KEY:-}" \
        "${REPLICATE_API_TOKEN:-}" \
        "${FAL_KEY:-}" \
        "${LUMA_API_KEY:-}"
    ok ".env file created from environment variables"

    # Report what was set
    [[ -n "${OPENAI_API_KEY:-}" ]]       && ok "OpenAI key configured"       || info "OpenAI key not provided"
    [[ -n "${XAI_API_KEY:-}" ]]          && ok "xAI key configured"          || info "xAI key not provided"
    [[ -n "${REPLICATE_API_TOKEN:-}" ]]  && ok "Replicate key configured"    || info "Replicate key not provided"
    [[ -n "${FAL_KEY:-}" ]]              && ok "FAL key configured"          || info "FAL key not provided"
    [[ -n "${LUMA_API_KEY:-}" ]]         && ok "Luma key configured"         || info "Luma key not provided"
else
    # Interactive: prompt for each key
    echo ""
    info "Let's set up your API keys."
    info "Press Enter to skip any key you don't have — you can add it later."
    echo ""

    read -rp "$(echo -e "${CYAN}OpenAI API Key${NC} (sk-proj-...): ")" INPUT_OPENAI
    read -rp "$(echo -e "${CYAN}xAI API Key${NC} (xai-...): ")" INPUT_XAI
    read -rp "$(echo -e "${CYAN}Replicate API Token${NC} (r8_...): ")" INPUT_REPLICATE
    read -rp "$(echo -e "${CYAN}FAL Key${NC}: ")" INPUT_FAL
    read -rp "$(echo -e "${CYAN}Luma API Key${NC}: ")" INPUT_LUMA

    write_env_file \
        "${INPUT_OPENAI:-}" \
        "${INPUT_XAI:-}" \
        "${INPUT_REPLICATE:-}" \
        "${INPUT_FAL:-}" \
        "${INPUT_LUMA:-}"

    echo ""
    ok ".env file created"
fi

# ── 5. Output directories ─────────────────────────────────

header "Output Directories"

for dir in output caption_output burn_output video-output; do
    if [[ -d "$dir" ]]; then
        ok "$dir/ exists"
    else
        mkdir -p "$dir"
        ok "Created $dir/"
    fi
done

# ── 6. Verification ───────────────────────────────────────

header "Verification"

ERRORS=0

# Check Python imports
if python -c "import fastapi, uvicorn, httpx, PIL, openai, dotenv" 2>/dev/null; then
    ok "All Python packages importable"
else
    fail "Some Python packages failed to import"
    ERRORS=$((ERRORS + 1))
fi

# Check ffmpeg
if ffmpeg -version &>/dev/null; then
    ok "ffmpeg works"
else
    fail "ffmpeg not working"
    ERRORS=$((ERRORS + 1))
fi

# Check fonts
if [[ -f "fonts/TikTokSans16pt-Bold.ttf" ]]; then
    ok "TikTok fonts present"
else
    fail "fonts/TikTokSans16pt-Bold.ttf missing — caption burn won't work"
    ERRORS=$((ERRORS + 1))
fi

# Check .env has at least one real key
if grep -q "^[A-Z].*_KEY=.\|^[A-Z].*_TOKEN=." .env 2>/dev/null; then
    ok ".env has at least one API key configured"
else
    warn ".env exists but no keys are set yet"
fi

echo ""
if [[ $ERRORS -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════${NC}"
    echo -e "${GREEN}${BOLD}  Setup complete! Everything looks good.       ${NC}"
    echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════${NC}"
else
    echo -e "${RED}${BOLD}═══════════════════════════════════════════════${NC}"
    echo -e "${RED}${BOLD}  Setup finished with $ERRORS error(s). Fix them above.${NC}"
    echo -e "${RED}${BOLD}═══════════════════════════════════════════════${NC}"
fi

echo ""
echo -e "${BOLD}To start the servers:${NC}"
echo ""
echo -e "  ${CYAN}source venv/bin/activate${NC}      # activate the virtual environment"
echo ""
echo -e "  ${CYAN}python server.py${NC}              # localhost:8000 — video generation"
echo -e "  ${CYAN}python caption_server.py${NC}      # localhost:8001 — caption scraping"
echo -e "  ${CYAN}python burn_server.py${NC}         # localhost:8002 — caption burning"
echo ""
echo -e "  Each server needs its own terminal tab."
echo ""
