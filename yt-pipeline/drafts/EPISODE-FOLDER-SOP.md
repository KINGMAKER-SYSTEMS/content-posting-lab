# Episode Folder SOP — the one schema every agent obeys

> **Read this before writing ANY file during episode work.** Applies to every agent —
> claude, pi, codex, ocean, subagents, all of them. The rules here are enforced in
> *code* (a runtime path-gateway that rejects off-schema writes) and, for Claude
> agents, by a pre-write hook. You do not have the option of dumping files wherever.
>
> **Last updated:** 2026-06-13 · Owner: john

---

## TL;DR (the five rules)

1. **Never hand-build an asset path.** Call the gateway: `services.abn_assets.asset_path(ep_id, kind, slug)`. It returns a validated, dir-created `Path`, or it raises.
2. **Every episode asset is scoped to its episode.** It lives under `agenticnews_assets/episodes/{ep_id}/…`, never loose at the asset-store root.
3. **Cross-episode assets are `shared/`.** Logos, music beds, the broll library, card backgrounds — `shared_path(category, name)`. Never duplicated per episode, never GC'd by episode pruning.
4. **Throwaway test output goes to a scratch dir** — `episodes/{ep_id}/scratch/` for episode probes, `_scratch/` for cross-episode experiments. The GC may reap scratch freely; it must never reap anything else without a tombstone.
5. **The URL form (`/agenticnews-assets/…`) is what goes into timelines and props.** Get it from `asset_url(...)`. Never a `file://` or absolute disk path — the headless render loads over HTTP and a disk path silently fails.

If you're an agent and you're unsure where a file goes: **it goes through `asset_path`/`shared_path`. If that raises, the file doesn't have a home yet — fix the call, don't invent a path.**

---

## The two storage zones (know which one you're in)

ABN work scatters across **two** different trees. Keep them straight.

### Zone A — the runtime ASSET STORE (rendered media)
This is `ASSETS_DIR` — resolved by `services/agenticnews.py` from `ABN_ASSETS_DIR`
env, else the Railway volume, else the repo's `agenticnews_assets/`. **Locally it is the
T9 SSD** (`/Volumes/T9/agenticbuildernews/agenticnews_assets`, fronted by a repo
symlink). This is where every generated layer asset / episode mp4 lands, and it's what
the app serves over HTTP at `/agenticnews-assets/`.

**The schema groups each episode by LAYER SOURCE** — because an ABN episode is
heterogeneous source layers composited by OpenShot, and these subdirs mirror
`openshot_bridge.SOURCE_TYPES` (`remotion / webscroll / css / broll / still / vo / bed`):

```
agenticnews_assets/                 (= ASSETS_DIR, on T9)
├── {ep_id}/                         e.g. ep_648e806a  (directly under the root)
│   ├── footage/      webscroll Playwright captures (ui.mp4)        → source webscroll
│   ├── css/          rasterized seekable-HTML cards + kinetic type → source css/still
│   ├── remotion/     terminal / code-graphics shots                → source remotion
│   ├── broll/        ambient plates, code demos, source stills      → source broll/still
│   ├── audio/        vo wavs, alignment json, ducked bed           → source vo/bed
│   ├── renders/      episode.mp4, thumb, assembled cuts            → output
│   ├── scratch/      per-episode intermediates — GC may reap
│   ├── timeline.json   render props — GROUND TRUTH for QA
│   └── manifest.json   asset index
├── _shared/          cross-episode (logos, library beds, broll_library, card_backgrounds)
│   ├── brand/   audio/   broll_library/   card_backgrounds/
├── _published/       shipped finals, per-episode subdir (cleanup target)
├── _scratch/         cross-episode test output (reapable)
└── _trash/           tombstones — GC moves here instead of deleting
```

### Zone B — the repo WORKING TREE (scripts, research, drafts)
This is `yt-pipeline/` itself — the authoring side. One directory per episode, named
**`drafts/{episode-slug}/`**, with a consistent internal layout. (Today these are
inconsistent — `ep-stack-split` vs `ep-agent-stack-splitting`. New work uses the
canonical layout below; old dirs get renamed as they're touched.)

```
yt-pipeline/drafts/{episode-slug}/
├── outline.json          the segment ledger (source of truth for the script)
├── research/             facetN.md research notes
├── scripts/              sN.md per-segment scripts
├── align/                ALIGN_sN/ word-timing artifacts
└── NOTES.md             session log / handoff for this episode
```

Agent scratch dirs (`RalphAgent1-…`, `work_sound2/`, etc.) are **ephemeral** — they
belong under a single `_agent-scratch/` dir, never committed, gitignored.

---

## The asset-path gateway (the enforced API)

`services/abn_assets.py` is the **only** sanctioned way to get a write path in Zone A.

```python
from services.abn_assets import asset_path, asset_url, shared_path, published_path

# write a card for segment 0 (a css-layer asset)
p = asset_path(ep_id, "card", "s0")          # -> {ep}/css/s0_card.png  (dir created)
img.save(p)

# the URL to put in the timeline
url = asset_url(ep_id, "kinetic", "s1")       # -> /agenticnews-assets/{ep}/css/s1_kinetic.mp4

# episode-root singleton + renders
tl  = asset_path(ep_id, "timeline")           # -> {ep}/timeline.json
mp4 = asset_path(ep_id, "episode")            # -> {ep}/renders/episode.mp4

# shared, cross-episode asset; and a shipped final
bed = shared_path("audio", "bed_v2.mp3")      # -> _shared/audio/bed_v2.mp3
fin = published_path(ep_id, "episode.mp4")    # -> _published/{ep}/episode.mp4
```

**The closed kind vocabulary** (`KINDS` in `abn_assets.py`) — these are the *only*
asset kinds, grouped by layer source. An unknown kind raises:

| kind | subdir | ext | layer source |
|---|---|---|---|
| `ui` `footage` `webscroll` | `footage/` | mp4 | webscroll (Playwright page footage) |
| `card` `hook` `number` `quote` `vs` `diagram` `kinetic` `css` | `css/` | png/mp4 | css (seekable-HTML cards + kinetic type) |
| `remotion` `terminal` | `remotion/` | mp4 | remotion (terminal / graphics shots) |
| `broll` `demo` `still` `src` | `broll/` | mp4/png | broll / still (plates, demos, screenshots) |
| `voice` `vo` `align` `bed` | `audio/` | wav/json/mp3 | vo + bed |
| `episode` `assembled` `thumb` `thumb_bg` | `renders/` | mp4/png | output |
| `timeline` `manifest` | (ep root) | json | props / index |
| `scratch` | `scratch/` | (any) | reapable intermediate |

**Adding a new asset type?** Register it in `abn_assets.KINDS` — and *only* there. Do
not hand-build a path to dodge the vocabulary; the gateway and the hook will reject it,
and the GC won't know how to protect it.

---

## Migrating the existing flat dump

The 879 legacy files are flat (`ep_648e806a_s0_card.png`). One-time migration:

```bash
python scripts/migrate_abn_assets.py            # dry run — show the plan
python scripts/migrate_abn_assets.py --report   # classification summary
python scripts/migrate_abn_assets.py --apply    # copy into the schema + leave back-compat symlinks
```

It **copies** (never moves), verifies byte-size, and leaves a **symlink at each old flat
path** so every existing timeline/props/in-flight render keeps resolving during cutover.
Nothing is deleted — reclaiming originals is a separate manual step after you've
confirmed renders still work.

---

## Why this is enforced in code, not just docs

System prompts get ignored, especially by subagents under load. So the schema is
enforced where it actually matters — at the write:

- **Runtime gateway (all agents):** `asset_path()` validates and raises on anything
  off-schema. pi / codex / ocean / claude all hit the same wall. You cannot get a bad
  path out of it.
- **Claude pre-write hook (claude agents):** a `PreToolUse` hook on `Write`/`Edit`/`Bash`
  (`.claude/hooks/abn_asset_guard.py`) blocks a stray write into the asset-store root
  (anything not under a real `{ep_id}/`, `_shared/`, `_published/`, `_scratch/`,
  `_trash/`) and tells you the gateway call to use instead.
- **GC safety (target):** the disk GC should reap only `{ep_id}/scratch/` and `_scratch/`,
  and move anything else to `_trash/` (tombstone) instead of unlinking — so the
  original-VO loss that happened under the old prefix-glob GC can't recur. *(The GC
  refactor lands with the abn_factory wiring — see the pipeline-takeover handoff.)*
