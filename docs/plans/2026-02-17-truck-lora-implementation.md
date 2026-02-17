# Truck UGC LoRA — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a data prep pipeline + server.py integration so you can train a Wan 2.1 LoRA that generates static, phone-quality UGC truck scenes and serve it through the existing FAL endpoint.

**Architecture:** Training data lives in `training_data/truck-ugc-v1/` (gitignored). A shared `prep_clips.py` script processes raw clips into Wan-compatible format. Training runs on RunPod via musubi-tuner. The resulting `.safetensors` is hosted on HuggingFace and loaded in `server.py` via a new `fal-wan-lora` provider entry using FAL's native `loras` param.

**Tech Stack:** Python 3.11+, ffmpeg (already on PATH), FAL.ai queue API, HuggingFace Hub CLI, musubi-tuner (RunPod), FastAPI (existing server.py)

---

## Task 1: Create training data directory structure

**Files:**
- Create: `training_data/.gitkeep`
- Create: `training_data/truck-ugc-v1/README.md`
- Modify: `.gitignore`

**Step 1: Add training_data/ to .gitignore**

Open `.gitignore` and add at the bottom:

```
# LoRA training data (large video files)
training_data/*/raw/
training_data/*/processed/
training_data/*/captions/
training_data/shared/__pycache__/
```

Note: We gitignore the video files but NOT `dataset.json` or `README.md` — those are small and worth tracking.

**Step 2: Create the directory tree**

```bash
mkdir -p training_data/truck-ugc-v1/raw
mkdir -p training_data/truck-ugc-v1/processed
mkdir -p training_data/truck-ugc-v1/captions
mkdir -p training_data/shared
touch training_data/.gitkeep
```

**Step 3: Write the dataset README**

Create `training_data/truck-ugc-v1/README.md`:

```markdown
# Truck UGC v1 — Training Dataset

**Model:** Wan 2.1 T2V LoRA
**Trigger token:** `TRUCKUGC`
**Target aesthetic:** Phone-quality UGC, parked trucks in fields, props on hood/tailgate, natural lighting

## Clip requirements
- Parked truck (no spinning wheels, no driving)
- Field setting (dirt/grass/gravel, wooden fenceline preferred)
- Props on truck (hats, gloves, tools, coolers, feed bags)
- Phone camera quality — grain, natural exposure, no color grade
- Natural/low light — golden hour, overcast, shade
- Static or near-static camera (no pans, no zooms, no drone)
- No watermarks

## Processed format
- Resolution: 480×848 (9:16)
- FPS: 16
- Duration: 2.5s (41 frames — follows Wan 4n+1 rule)
- Format: MP4 H.264

## Sources
| Account/Source | URL | Clips pulled | Notes |
|----------------|-----|--------------|-------|
| (fill in as you scrape) | | | |

## Caption template
`TRUCKUGC, [color] [make/model] pickup truck parked [setting], [props visible], [lighting], phone camera quality, static shot`
```

**Step 4: Commit**

```bash
git add .gitignore training_data/truck-ugc-v1/README.md training_data/.gitkeep
git commit -m "feat: add training_data directory structure for truck UGC LoRA"
```

---

## Task 2: Build the clip preprocessing script

This is the reusable tool that takes raw MP4s → Wan-ready training clips. It handles resize, FPS conversion, trimming to valid frame count (4n+1 rule), and naming.

**Files:**
- Create: `training_data/shared/prep_clips.py`

**Step 1: Write prep_clips.py**

```python
#!/usr/bin/env python3
"""
prep_clips.py — Preprocess raw video clips into Wan 2.1 training format.

Usage:
    python training_data/shared/prep_clips.py \
        --input  training_data/truck-ugc-v1/raw \
        --output training_data/truck-ugc-v1/processed \
        --width 480 --height 848 --fps 16 --frames 41

Output per clip:
    processed/clip_001.mp4   ← resized, 16fps, exactly 41 frames
    captions/clip_001.txt    ← placeholder (you fill in)

The 4n+1 rule for Wan frame counts: 9, 13, 17, 21, 25, 33, 41, 81
Default is 41 frames = 2.5625 seconds at 16fps.
"""

import argparse
import subprocess
import sys
from pathlib import Path


VALID_FRAME_COUNTS = [9, 13, 17, 21, 25, 33, 41, 81]


def nearest_valid_frames(requested: int) -> int:
    """Round to the nearest valid Wan frame count (4n+1 rule)."""
    return min(VALID_FRAME_COUNTS, key=lambda x: abs(x - requested))


def get_video_duration(path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def process_clip(
    src: Path,
    dest: Path,
    width: int,
    height: int,
    fps: int,
    frames: int,
) -> bool:
    """
    Process one clip: resize → set FPS → trim to exactly N frames.
    Returns True on success, False on failure.
    """
    duration_secs = frames / fps  # e.g. 41/16 = 2.5625s

    # Scale to fill target resolution, then crop center to exact size.
    # This handles source clips that are landscape or wrong aspect ratio.
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"fps={fps}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", vf,
        "-t", str(duration_secs),
        "-c:v", "libx264",
        "-crf", "18",          # high quality for training data
        "-preset", "fast",
        "-an",                  # no audio (not needed for training)
        str(dest),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[-300:]}", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Prep raw clips for Wan LoRA training")
    parser.add_argument("--input",   required=True, help="Directory of raw MP4s")
    parser.add_argument("--output",  required=True, help="Directory for processed clips")
    parser.add_argument("--captions", default=None, help="Directory for caption .txt files (default: ../captions relative to output)")
    parser.add_argument("--width",   type=int, default=480)
    parser.add_argument("--height",  type=int, default=848)
    parser.add_argument("--fps",     type=int, default=16)
    parser.add_argument("--frames",  type=int, default=41,
                        help=f"Frame count (valid: {VALID_FRAME_COUNTS})")
    args = parser.parse_args()

    frames = nearest_valid_frames(args.frames)
    if frames != args.frames:
        print(f"⚠ Rounding frame count {args.frames} → {frames} (4n+1 rule)")

    input_dir  = Path(args.input)
    output_dir = Path(args.output)
    caption_dir = Path(args.captions) if args.captions else output_dir.parent / "captions"

    output_dir.mkdir(parents=True, exist_ok=True)
    caption_dir.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        p for p in input_dir.iterdir()
        if p.suffix.lower() in {".mp4", ".mov", ".avi", ".webm"}
    )

    if not sources:
        print(f"No video files found in {input_dir}")
        sys.exit(1)

    print(f"Processing {len(sources)} clips → {output_dir}")
    print(f"Settings: {args.width}×{args.height}, {fps_str(args.fps)}, {frames} frames ({frames/args.fps:.2f}s)\n")

    ok, fail = 0, 0
    for i, src in enumerate(sources, start=1):
        name = f"clip_{i:03d}"
        dest = output_dir / f"{name}.mp4"
        caption_file = caption_dir / f"{name}.txt"

        print(f"[{i}/{len(sources)}] {src.name} → {dest.name}", end=" ... ")
        if process_clip(src, dest, args.width, args.height, args.fps, frames):
            print("✓")
            ok += 1
        else:
            print("✗ FAILED")
            fail += 1

        # Create empty caption file if it doesn't exist
        if not caption_file.exists():
            caption_file.write_text(
                f"TRUCKUGC, [describe: truck color/make, setting, props, lighting, phone quality, static shot]\n"
            )

    print(f"\nDone: {ok} succeeded, {fail} failed")
    print(f"Caption files written to: {caption_dir}")
    print("→ Open each .txt file and fill in the actual caption for that clip.")


def fps_str(fps: int) -> str:
    return f"{fps}fps"


if __name__ == "__main__":
    main()
```

**Step 2: Make it executable and test it shows help**

```bash
chmod +x training_data/shared/prep_clips.py
python training_data/shared/prep_clips.py --help
```

Expected output:
```
usage: prep_clips.py [-h] --input INPUT --output OUTPUT ...
```

**Step 3: Commit**

```bash
git add training_data/shared/prep_clips.py
git commit -m "feat: add prep_clips.py for Wan training data preprocessing"
```

---

## Task 3: Build the dataset.json generator

After clips are processed and captions are written, this script generates the `dataset.json` training manifest that musubi-tuner reads.

**Files:**
- Create: `training_data/shared/make_dataset.py`

**Step 1: Write make_dataset.py**

```python
#!/usr/bin/env python3
"""
make_dataset.py — Generate dataset.json manifest for musubi-tuner Wan LoRA training.

Usage:
    python training_data/shared/make_dataset.py \
        --processed training_data/truck-ugc-v1/processed \
        --captions  training_data/truck-ugc-v1/captions \
        --output    training_data/truck-ugc-v1/dataset.json \
        --fps 16

The output dataset.json is what you upload to RunPod alongside the clips.
"""

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Generate musubi-tuner dataset.json")
    parser.add_argument("--processed", required=True, help="Directory of processed MP4s")
    parser.add_argument("--captions",  required=True, help="Directory of .txt captions")
    parser.add_argument("--output",    required=True, help="Output dataset.json path")
    parser.add_argument("--fps",       type=int, default=16)
    args = parser.parse_args()

    processed_dir = Path(args.processed)
    captions_dir  = Path(args.captions)
    output_path   = Path(args.output)

    clips = sorted(processed_dir.glob("*.mp4"))
    if not clips:
        print(f"No MP4s found in {processed_dir}")
        sys.exit(1)

    entries = []
    missing_captions = []

    for clip in clips:
        caption_file = captions_dir / f"{clip.stem}.txt"
        if not caption_file.exists():
            missing_captions.append(clip.name)
            continue

        caption = caption_file.read_text().strip()
        if not caption or "[describe:" in caption:
            print(f"⚠ Skipping {clip.name} — caption not filled in yet")
            continue

        entries.append({
            "video": str(clip.resolve()),
            "caption": caption,
            "fps": args.fps,
        })

    if missing_captions:
        print(f"⚠ Missing caption files for: {', '.join(missing_captions)}")

    if not entries:
        print("No valid entries found. Fill in caption .txt files first.")
        sys.exit(1)

    output_path.write_text(json.dumps(entries, indent=2))
    print(f"✓ Wrote {len(entries)} entries to {output_path}")

    if missing_captions or len(entries) < len(clips):
        print(f"  ({len(clips) - len(entries)} clips skipped — unfilled captions)")


if __name__ == "__main__":
    main()
```

**Step 2: Test shows help**

```bash
python training_data/shared/make_dataset.py --help
```

Expected: usage line prints, no errors.

**Step 3: Commit**

```bash
git add training_data/shared/make_dataset.py
git commit -m "feat: add make_dataset.py to generate musubi-tuner training manifest"
```

---

## Task 4: Add the fal-wan-lora provider to server.py

This is the code change that lets your trained LoRA be selected in the UI and called through FAL.

**Files:**
- Modify: `server.py` (3 targeted edits)

**Step 1: Add the new provider to the PROVIDERS dict**

In `server.py`, find the `"fal-wan"` entry in `PROVIDERS` (around line 398). Add the new provider immediately after the closing brace of `"fal-wan"`:

Find:
```python
    "fal-wan": {
        "name": "Wan 2.5 (FAL)",
        "key_id": "fal",
        "pricing": "$0.05/sec",
        "models": ["fal-ai/wan-25-preview/text-to-video"],
    },
```

Add after it:
```python
    "fal-wan-lora": {
        "name": "Wan 2.1 LoRA (Truck UGC)",
        "key_id": "fal",
        "pricing": "$0.05/sec",
        "models": ["fal-ai/wan/v2.1/t2v/lora"],
    },
```

**Step 2: Add LoRA support to `_fal_generate`**

In `_fal_generate` (around line 109), find the payload block for the `"wan"` model and add a `loras` field when `TRUCK_LORA_URL` is set.

Find this block (around line 112):
```python
    # Model-specific params
    if "wan" in model_id:
        payload["duration"] = str(duration) if duration <= 10 else "10"
        payload["resolution"] = resolution
        payload["aspect_ratio"] = aspect_ratio
```

Replace with:
```python
    # Model-specific params
    if "wan" in model_id:
        payload["duration"] = str(duration) if duration <= 10 else "10"
        payload["resolution"] = resolution
        payload["aspect_ratio"] = aspect_ratio
        # Inject LoRA weights if configured
        lora_url = os.getenv("TRUCK_LORA_URL")
        if lora_url and "lora" in model_id:
            payload["loras"] = [{
                "path": lora_url,
                "scale": float(os.getenv("TRUCK_LORA_SCALE", "1.0")),
            }]
```

**Step 3: Add fal-wan-lora to the model_map in `_generate_one`**

Find the `model_map` dict in `_generate_one` (around line 494):

```python
                model_map = {
                    "fal-wan": "fal-ai/wan-25-preview/text-to-video",
                    "fal-kling": "fal-ai/kling-video/v2.5-turbo/pro",
                    "fal-ovi": "fal-ai/ovi",
                }
```

Replace with:
```python
                model_map = {
                    "fal-wan": "fal-ai/wan-25-preview/text-to-video",
                    "fal-wan-lora": "fal-ai/wan/v2.1/t2v/lora",
                    "fal-kling": "fal-ai/kling-video/v2.5-turbo/pro",
                    "fal-ovi": "fal-ai/ovi",
                }
```

**Step 4: Verify the server starts clean**

```bash
source venv/bin/activate
python server.py &
sleep 3
curl -s http://localhost:8000/api/providers | python -m json.tool | grep -A3 "wan-lora"
kill %1
```

Expected: `"fal-wan-lora"` appears in the providers list (it will only show if `FAL_KEY` is set in `.env`).

**Step 5: Commit**

```bash
git add server.py
git commit -m "feat: add fal-wan-lora provider with conditional LoRA weight injection"
```

---

## Task 5: Add TRUCK_LORA_URL to .env (after training)

This task happens AFTER you've trained and uploaded the LoRA to HuggingFace.

**Files:**
- Modify: `.env`

**Step 1: Add the new variables**

Open `.env` and add:

```env
# Truck UGC LoRA — Wan 2.1
# Set this after uploading truck_ugc_v1.safetensors to HuggingFace
TRUCK_LORA_URL=https://huggingface.co/risingtides-dev/truck-ugc-wan21/resolve/main/truck_ugc_v1.safetensors
TRUCK_LORA_SCALE=1.0
```

**Step 2: Verify FAL picks it up**

```bash
source venv/bin/activate
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.getenv('TRUCK_LORA_URL'))"
```

Expected: Prints the HuggingFace URL.

Note: `.env` is gitignored. This step is manual — no commit needed.

---

## Task 6: RunPod training runbook

This is a step-by-step terminal runbook to execute on RunPod. Not code to write — actions to take.

**Step 1: Launch a RunPod pod**

- Go to runpod.io → Pods → New Pod
- Template: `RunPod Pytorch 2.4.0` (or latest)
- GPU: `RTX 4090` (cheapest with 24GB VRAM)
- Storage: 50GB (enough for base model + training data)
- Click Deploy → Connect via web terminal

**Step 2: Clone musubi-tuner and install deps**

```bash
cd /workspace
git clone https://github.com/kohya-ss/musubi-tuner.git
cd musubi-tuner
pip install -r requirements.txt
pip install huggingface_hub
```

**Step 3: Download Wan 2.1 base model weights**

```bash
cd /workspace
mkdir -p wan2.1
huggingface-cli download Wan-AI/Wan2.1-T2V-14B \
    --local-dir wan2.1 \
    --include "*.safetensors" "*.json"
```

This downloads ~28GB. Takes 10–15 min on RunPod's fast connection.

**Step 4: Upload your training data**

From your local machine (in a separate terminal):

```bash
# Zip up processed clips + captions + dataset.json
cd content-posting-lab/training_data/truck-ugc-v1
zip -r truck-ugc-v1-training.zip processed/ captions/ dataset.json

# Upload to RunPod via their file uploader, or use scp:
scp -P <PORT> truck-ugc-v1-training.zip root@<RUNPOD_IP>:/workspace/
```

Then on RunPod:
```bash
cd /workspace
unzip truck-ugc-v1-training.zip -d training_data/
```

**Step 5: Write the training config**

On RunPod, create `/workspace/truck_ugc_train.toml`:

```toml
[general]
enable_bucket = true
resolution = [480, 848]

[[datasets]]
video_directory = "/workspace/training_data/processed"
caption_extension = ".txt"
caption_directory = "/workspace/training_data/captions"
target_frames = [41]
frame_extraction = "head"
fps = 16

[model]
model_path = "/workspace/wan2.1"
model_type = "wan_video"

[network]
network_module = "networks.lora"
network_dim = 16
network_alpha = 8

[optimizer]
optimizer_type = "AdamW8bit"
learning_rate = 1e-4
lr_scheduler = "cosine_with_restarts"
lr_warmup_steps = 100

[training]
max_train_steps = 2000
batch_size = 2
save_every_n_steps = 500
output_dir = "/workspace/output"
output_name = "truck_ugc_v1"
mixed_precision = "bf16"
gradient_checkpointing = true
```

**Step 6: Run training**

```bash
cd /workspace/musubi-tuner
python wan_train_network.py \
    --config_file /workspace/truck_ugc_train.toml
```

Watch the loss. It should decrease from ~1.0 to ~0.1–0.3 over 2000 steps. If it plateaus above 0.5, the captions may be too generic.

**Step 7: Download the output**

```bash
# From local machine:
scp -P <PORT> root@<RUNPOD_IP>:/workspace/output/truck_ugc_v1.safetensors \
    training_data/truck-ugc-v1/truck_ugc_v1.safetensors
```

**Immediately stop the pod after download** — you're billed by the hour.

---

## Task 7: Upload LoRA to HuggingFace

**Step 1: Create the HuggingFace repo**

```bash
pip install huggingface_hub
huggingface-cli login   # enter your HF token
huggingface-cli repo create truck-ugc-wan21 --type model
```

**Step 2: Upload the weights**

```bash
huggingface-cli upload risingtides-dev/truck-ugc-wan21 \
    training_data/truck-ugc-v1/truck_ugc_v1.safetensors \
    truck_ugc_v1.safetensors
```

**Step 3: Verify the URL works**

```bash
curl -I "https://huggingface.co/risingtides-dev/truck-ugc-wan21/resolve/main/truck_ugc_v1.safetensors"
```

Expected: `HTTP/2 200` (or `302` redirect — FAL will follow it).

**Step 4: Update .env with the live URL**

```env
TRUCK_LORA_URL=https://huggingface.co/risingtides-dev/truck-ugc-wan21/resolve/main/truck_ugc_v1.safetensors
```

---

## Task 8: End-to-end smoke test

Test that the full pipeline works: prompt → LoRA generation → output dir → burn-ready.

**Step 1: Start server 1**

```bash
source venv/bin/activate && python server.py
```

**Step 2: Submit a test generation via curl**

```bash
curl -s -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "TRUCKUGC, dark red Ford F-150 parked on a dirt road beside a wooden fence, worn leather gloves on the tailgate, golden hour light, phone camera quality, static shot",
    "provider": "fal-wan-lora",
    "count": 1,
    "duration": 5,
    "aspect_ratio": "9:16"
  }' | python -m json.tool
```

Note the `job_id` in the response.

**Step 3: Poll until done**

```bash
JOB_ID="<job_id_from_above>"
watch -n 5 "curl -s http://localhost:8000/api/jobs/$JOB_ID | python -m json.tool | grep status"
```

Expected: status transitions `generating → polling → downloading → done`

**Step 4: Verify output file exists**

```bash
ls output/fal-wan-lora/
```

Expected: `truckugc_dark_red.../` directory containing a `.mp4` file.

**Step 5: Open the video and visually inspect**

Check for:
- [ ] No spinning tires
- [ ] No warping body panels
- [ ] Truck appears parked / static
- [ ] Phone-quality look (no oversaturated cinematic grade)
- [ ] 9:16 aspect ratio

If the LoRA is underperforming (artifacts still present), try adjusting `TRUCK_LORA_SCALE` down to `0.7` in `.env` and retest.

---

## Prompt Reference Card

Save this somewhere handy — it's the formula for every truck generation.

**Template:**
```
TRUCKUGC, [color] [year?] [make] [model] parked [setting], [props on truck], [lighting condition], phone camera quality, static shot, UGC style
```

**Negative prompt (always use this):**
```
spinning wheels, motion blur, camera pan, drone shot, cinematic lighting, studio lighting, CGI, unrealistic, warped body panels, morphing
```

**Example prompts:**
```
TRUCKUGC, faded black Chevy Silverado 1500 parked on gravel beside a wooden fence post, a Carhartt beanie and pair of work gloves on the hood, overcast afternoon light, phone camera quality, static shot, UGC style

TRUCKUGC, dark green Ford F-250 parked in a dry grass field, rusted toolbox and coiled rope in the truck bed, golden hour backlight, phone camera quality, static shot, UGC style

TRUCKUGC, white Ram 1500 tailgate down parked on a dirt road, worn leather belt and cowboy hat laid out on tailgate, soft cloudy light, phone camera quality, static shot, UGC style
```

---

## Iteration Notes

- **If LoRA is too strong** (loses realism, looks stylized): lower `TRUCK_LORA_SCALE` to 0.6–0.8
- **If motion artifacts persist**: increase dataset to 40+ clips, retrain with 3000 steps
- **v2 training**: add the v1 weights as a starting point (use `--pretrained_lora` in musubi-tuner) to build on v1 instead of starting from scratch
- All training data in `training_data/truck-ugc-v1/` is the long-term asset — keep it as you iterate

