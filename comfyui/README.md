# ComfyUI — Hyperreal UGC for TikTok (trucks / boats / "shot on a phone")

Standalone ComfyUI workflows for generating UGC-style content that reads as **real footage filmed on someone's cell phone**, not polished AI. Two stages:

1. **Stills** (`workflows/ugc_stills_sdxl.json`) — text → hyperreal vertical photo.
2. **Video** (`workflows/ugc_i2v_wan.json`) — that photo → 5s vertical clip (image-to-video).

> These are a **lab / exploration** drop. They do not touch the running app yet. Integration into `routers/video.py` as a provider is a later step (see *Integrating later* below).

---

## The one thing that matters: beat the "AI look"

Every base model defaults to the *opposite* of phone footage — cinematic, evenly lit, airbrushed, tripod-stable, hyper-sharp. **Most of your realism comes from deliberately degrading toward "phone camera," not from chasing fidelity.** Three levers, in order of impact:

1. **Prompt away from "good photography."** Say `amateur cell phone photo, snapshot, slightly overexposed, motion blur` and negative-prompt `cinematic, studio lighting, 8k, bokeh, professional`.
2. **An amateur/phone-photo LoRA** (stills) — single biggest quality jump.
3. **Phone-ify post** (grain, compression, mild softness, handheld feel) — see below.

---

## Install

### 1. ComfyUI
Use a recent build (WAN nodes change often — update if the video graph shows red/missing nodes):
```bash
git clone https://github.com/comfyanonymous/ComfyUI
cd ComfyUI && pip install -r requirements.txt
```

### 2. Custom nodes (via ComfyUI-Manager → Install Missing Custom Nodes)
| Node pack | Needed for | Why |
|---|---|---|
| **ComfyUI-VideoHelperSuite** | video | `VHS_VideoCombine` → mp4 export for TikTok |
| **ComfyUI_ProPost** | phone-ify | best film-grain / vignette node |
| ComfyUI-Frame-Interpolation *(optional)* | video | RIFE → smoother 16fps→30fps |

The stills graph uses **only core nodes** — it imports and runs with zero custom packs.

### 3. Models — download to these folders
**Stills (SDXL):**
- `models/checkpoints/` → a realism checkpoint, e.g. **RealVisXL V5.0** or **epiCRealism XL**.
- `models/loras/` → an **amateur / phone-photo SDXL LoRA** (search Civitai for "amateur photo", "iPhone photo", "instagram realism").

**Video (WAN 2.2 Image-to-Video, 14B):** drop into the matching folder:
- `models/diffusion_models/` → `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors`
- `models/text_encoders/` → `umt5_xxl_fp8_e4m3fn_scaled.safetensors`
- `models/vae/` → `wan_2.1_vae.safetensors`
- `models/clip_vision/` → `clip_vision_h.safetensors`

All are on the official ComfyUI / Comfy-Org HuggingFace repos. The filenames in the workflow are placeholders — **rename in the loader nodes to match what you actually downloaded.**

---

## VRAM tiers (the default targets ~12–16GB)

| VRAM | Stills | Video |
|---|---|---|
| **24GB+** (4090/3090) | Flux.1-dev or SDXL @ 1024 base | WAN 2.2 14B @ 720×1280, 81 frames |
| **12–16GB** (default) | SDXL @ 832×1216 | WAN 2.2 14B **fp8** @ 480×832, 81 frames |
| **8–10GB** | SDXL @ 768×1152 | WAN **GGUF** (Q4/Q5) via ComfyUI-GGUF, or **LTX-Video** for speed |

For low VRAM also add `--lowvram` to the ComfyUI launch flags and consider a 2-step Lightning/LCM LoRA.

---

## Using the workflows

### Stills (`ugc_stills_sdxl.json`)
1. Load it, set your checkpoint + LoRA filenames in the two left nodes.
2. Edit the **positive** prompt for your subject. Template:
   > `amateur cell phone photo, vertical snapshot of {SUBJECT}, {SETTING}, {TIME/WEATHER}, candid, slightly tilted framing, mild motion blur, visible sensor noise, posted to social media, iphone photo`
3. Keep the **negative** as shipped (it's the anti-cinematic block).
4. `832×1216` = 9:16-ish for TikTok. CFG is intentionally **low (4.5)** — high CFG looks "AI". Queue.

Subject swaps: `a lifted pickup truck on a gravel lot` · `a bass boat on a trailer in a driveway` · `a center-console boat docked at a marina, gulls`.

### Video (`ugc_i2v_wan.json`)
1. Put a still (from stage 1) in `ComfyUI/input/`, select it in **LoadImage**.
2. Fix the four loader filenames.
3. **Motion prompt describes what MOVES, not how it looks** — the still already set the look:
   > `handheld phone video, {SUBJECT ACTION}, subtle camera shake, casual home-video feel, slight wobble`
   - truck: `exhaust drifts, person walks around it, camera bobs`
   - boat: `gentle rocking on water, wake ripples, reflections shimmer, horizon tilts slightly`
4. `length: 81` @ 16fps ≈ 5s. Output is `output/ugc_clip_*.mp4`.

> **If WAN nodes import red:** your ComfyUI predates this template. Update ComfyUI, or open *Workflow → Templates → Video → Wan Image-to-Video* and paste the prompts/resolution/sampler settings above into it.

---

## Phone-ify post chain (the realism finisher)

Run this on the rendered clip (or graft it between `VAEDecode` and `VHS_VideoCombine`). Each step undoes a tell-tale "AI/clean" signal:

| Step | Node | Setting | Kills the tell |
|---|---|---|---|
| 1. Sensor grain | `ProPostFilmGrain` | grain 0.4–0.7, sat 0.3 | too-clean pixels |
| 2. Softness | `ImageScale` ×2: down to ~0.6×, back up (bilinear) | — | over-sharp AI edges |
| 3. Vignette | `ProPostVignette` | intensity ~0.2 | uniform brightness |
| 4. Compression | `VHS_VideoCombine` | `crf: 26–28`, `pix_fmt: yuv420p` | pristine bitrate |
| 5. *(opt)* shake | handheld-cam LoRA / motion prompt | subtle | gimbal-perfect stability |

**Rule of thumb:** apply less than feels right. The goal is "phone in bad light," not "broken VHS tape." CRF 26–28 + light grain does 80% of the work.

---

## Prompt cheat-sheet

**Always include (positive):** `amateur, cell phone photo / handheld phone video, vertical, candid, snapshot, slightly overexposed OR underexposed, motion blur, natural daylight, iphone`

**Always exclude (negative):** `cinematic, professional, studio lighting, 8k, hdr, dramatic, bokeh, depth of field, color graded, drone, gimbal, render, cgi, 3d, airbrushed, smooth, oversaturated, perfect, illustration, watermark, text`

**Realism dials:** CFG low (4–5.5) · grain on · slight under/overexposure · imperfect framing · everyday backgrounds (driveways, gravel lots, marinas, parking lots) beat scenic ones.

---

## Integrating later (into content-posting-lab)

When ready, ComfyUI exposes `POST /prompt` (queue) + `GET /history/{id}` over its HTTP API. The path of least resistance:
- Run ComfyUI as a sidecar; add a `comfyui` provider in `routers/video.py` that POSTs the **API-format** export of these graphs (ComfyUI → *Save (API Format)*) with prompt/seed templated per request, polls `/history`, and pulls the mp4.
- Keep generated clips project-scoped (`?project=`) like the other providers, then they flow into the existing clip → burn → Telegram/Postiz pipeline unchanged.

This README + the two graphs are the standalone starting point; no app code changed yet.
