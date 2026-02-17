# Truck UGC LoRA — Design Document
**Date:** 2026-02-17
**Target model:** Wan 2.1 (T2V)
**Integration:** FAL.ai `fal-ai/wan-i2v-lora` → existing `server.py`
**Goal:** Generate static, phone-quality UGC-style truck scenes (parked trucks in fields, wooden fencelines, props on hood/tailgate) without the motion hallucinations current Wan generations produce.

---

## Problem Statement

Current Wan generations of trucks produce:
- Spinning tires on parked vehicles
- Warped/morphing body panels
- Unnatural camera drift
- Over-cinematic lighting

We need the model to understand: **parked truck + field + props = static scene, minimal motion, phone camera quality.**

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     DATA PIPELINE                           │
│                                                             │
│  TikTok Scraper (existing)  +  Manual clips                 │
│         ↓                                                   │
│  training_data/                                             │
│    raw/          ← original downloads                       │
│    processed/    ← trimmed, resized, 16fps MP4s             │
│    captions/     ← per-clip .txt caption files              │
│    dataset.json  ← training manifest                        │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                    TRAINING (RunPod)                        │
│                                                             │
│  Framework: musubi-tuner (Wan-native LoRA trainer)          │
│  GPU: RTX 4090 (24GB) — best price/perf for ~30 clips       │
│  Duration: ~4-6 hours                                       │
│  Output: truck_ugc_v1.safetensors                           │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                  HOSTING (HuggingFace Hub)                  │
│                                                             │
│  Repo: risingtides-dev/truck-ugc-wan21                      │
│  File: truck_ugc_v1.safetensors                             │
│  Access: public or private (both work with FAL)             │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│               INFERENCE (FAL.ai via server.py)              │
│                                                             │
│  Endpoint: fal-ai/wan-i2v-lora                              │
│  Param: loras: [{ path: "hf://...", scale: 1.0 }]           │
│  Trigger token: "TRUCKUGC" in prompt                        │
│  Output: drops into existing output/ dir → burn pipeline    │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Dataset Curation

### Target aesthetic (training signal)
Every clip must show:
- **Truck parked** — no motion, no driving, no spinning wheels
- **Field setting** — dirt, grass, or gravel. Wooden fenceline preferred but not required for every clip
- **Props** — hats, gloves, toolboxes, rope, coolers, feed bags on hood/tailgate/bed
- **Phone quality** — slight grain, natural exposure, no color grading, no drone shots
- **Low/natural lighting** — golden hour, overcast, shade. Not midday harsh sun
- **No people** — or people only partially in frame (hand placing prop, walking away)

### Dataset size target
- **Minimum:** 20 clips (will produce a workable LoRA)
- **Target:** 30–40 clips (better generalization across truck makes/colors)
- **Max useful:** ~60 clips before diminishing returns

### Source mix
| Source | Volume | Notes |
|--------|--------|-------|
| TikTok scrape | ~60% | Use existing scraper on 3–5 target accounts |
| Manual filming | ~40% | Control quality, nail the exact look |

### TikTok accounts to target (scrape)
Look for accounts posting: truck lifestyle, ranch/farm trucks, working truck aesthetic.
Search hashtags: `#worktruck`, `#trucklife`, `#ranchlife`, `#farmtruck`, `#truckbed`

### Clip processing spec
| Property | Value |
|----------|-------|
| Resolution | 480×848 (9:16 portrait) or 848×480 (landscape, crop later) |
| FPS | 16fps |
| Duration | 2–5 seconds (41–81 frames, follow 4n+1 rule) |
| Format | MP4, H.264 |
| Watermarks | Must be removed — disqualifies clip |
| Motion | Camera must be still or very slight drift. No pans, no zooms |

### Caption format
Each clip gets a `.txt` caption file. Captions should be:
- Descriptive and specific, not generic
- Include: truck color, truck type, setting, props visible, lighting
- Include trigger token: `TRUCKUGC`

**Example caption:**
```
TRUCKUGC, a dark green pickup truck parked in a grassy field beside a wooden fence,
a worn leather cowboy hat and pair of work gloves resting on the hood,
golden hour light, phone camera quality, static shot
```

**Bad caption (too generic):**
```
a truck in a field
```

---

## Phase 2: Training

### Framework
**musubi-tuner** — the de facto standard for Wan LoRA training. Purpose-built for Wan's DiT architecture (not kohya, which is UNet-based).

Repo: `https://github.com/kohya-ss/musubi-tuner`

### Environment (RunPod)
- **Pod type:** RTX 4090 (24GB) — sufficient for 480p, batch size 1–2
- **Template:** PyTorch 2.x + CUDA 12.x
- **Estimated cost:** ~$0.74/hr × 5 hours = ~$3.70 per training run

### Key hyperparameters
| Parameter | Value | Reasoning |
|-----------|-------|-----------|
| Rank (r) | 16 | Sufficient for a scene/aesthetic LoRA |
| Alpha (α) | 8 | Half of rank, standard ratio |
| Learning rate | 1e-4 | Safe starting point |
| Steps | 2000 | For ~30 clips at 480p |
| Batch size | 2 | Fits 24GB at 480p |
| Scheduler | cosine with warmup | 5% warmup (100 steps) |
| Clip frames | 41 (2.5s at 16fps) | Standard Wan frame count |
| Resolution | 480×848 | 9:16 portrait for TikTok |

### Training data structure (on RunPod)
```
/workspace/
├── wan2.1/               ← base model weights (download from HF)
├── musubi-tuner/         ← training framework
└── training_data/
    ├── processed/        ← MP4 clips
    ├── captions/         ← matching .txt files
    └── dataset.json      ← training manifest
```

### Output
`truck_ugc_v1.safetensors` — download immediately after training, upload to HuggingFace.

---

## Phase 3: Hosting

**HuggingFace Hub** — free, permanent, URL-accessible.

```
risingtides-dev/truck-ugc-wan21
├── truck_ugc_v1.safetensors
├── README.md              ← trigger token, example prompts, settings
└── sample_outputs/        ← before/after comparison videos
```

Direct URL for FAL:
```
https://huggingface.co/risingtides-dev/truck-ugc-wan21/resolve/main/truck_ugc_v1.safetensors
```

---

## Phase 4: Integration into server.py

The existing FAL provider function in `server.py` needs one small addition: a `loras` field in the payload when a LoRA URL is configured.

### New `.env` variable
```env
# Truck UGC LoRA (Wan 2.1)
TRUCK_LORA_URL=https://huggingface.co/risingtides-dev/truck-ugc-wan21/resolve/main/truck_ugc_v1.safetensors
TRUCK_LORA_SCALE=1.0
```

### Change to server.py
The FAL payload for the Wan endpoint gains:
```python
"loras": [
    {
        "path": os.getenv("TRUCK_LORA_URL"),
        "scale": float(os.getenv("TRUCK_LORA_SCALE", "1.0"))
    }
]
```

This is added conditionally — only if `TRUCK_LORA_URL` is set — so existing behavior is unchanged when the env var isn't present.

### UI change
A checkbox in the video gen UI: "Use Truck UGC LoRA" — visible only when the FAL Wan provider is selected.

---

## Training Data Directory (Reusable for Future LoRAs)

All training data lives in `training_data/` in the project root (gitignored). Structure is designed to be reusable across future LoRA projects:

```
training_data/
├── truck-ugc-v1/
│   ├── raw/              ← original downloaded clips (archival)
│   ├── processed/        ← training-ready MP4s (16fps, 480p, trimmed)
│   ├── captions/         ← .txt caption per clip (same filename as MP4)
│   ├── dataset.json      ← training manifest
│   └── README.md         ← what's in here, scrape sources, notes
├── [future-lora-name]/
└── shared/
    └── prep_clips.py     ← shared preprocessing script (ffmpeg wrapper)
```

The `prep_clips.py` script handles: resize → FPS conversion → trim to valid frame count → output naming. Reused for every future LoRA project.

---

## Prompt Engineering (Post-Training)

Always include the trigger token `TRUCKUGC` in the prompt. Recommended prompt structure:

```
TRUCKUGC, [truck description], parked in [setting], [props], [lighting],
phone camera quality, static shot, UGC style
```

**Example production prompt:**
```
TRUCKUGC, black Ford F-250 Super Duty parked on a dirt road beside a weathered wooden fence,
worn leather work gloves and a Carhartt beanie on the tailgate, late afternoon overcast light,
phone camera quality, static shot, UGC style
```

**Negative prompt (always use):**
```
spinning wheels, motion blur, camera pan, drone shot, cinematic, studio lighting,
CGI, unrealistic, warped, morphing
```

---

## Iteration Plan

| Version | Dataset | Change | Goal |
|---------|---------|--------|------|
| v1 | 30 clips | Baseline | Eliminate motion artifacts |
| v2 | +20 clips | More prop variety | Better generalization |
| v3 | Retrain on 2.2 | Architecture update | Quality improvement |

Save all training data between versions — the dataset is the long-term asset.

---

## What We're NOT Building (Scope Cuts)

- No automated scraping pipeline for training data (manual curation is fine at this scale)
- No training UI (RunPod terminal is sufficient)
- No automated evaluation/comparison tooling
- Not targeting Wan 2.2 yet (LoRA ecosystem less mature, MoE breaks 2.1 weights)

---

## Success Criteria

- Generated truck videos have no spinning tires or body panel warping
- Scene remains static (truck parked, minimal camera drift)
- Maintains phone-quality UGC aesthetic (grain, natural exposure)
- LoRA loads cleanly via FAL endpoint with no errors
- Generated videos drop into existing burn pipeline without changes

