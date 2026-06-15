#!/usr/bin/env python3
"""
PreToolUse hook — block stray writes into the ABN asset store.

Enforces the Episode Folder SOP (drafts/EPISODE-FOLDER-SOP.md) for CLAUDE-family
agents: any Write / Edit / NotebookEdit / Bash that would create a file at the ROOT of
the asset store (instead of under episodes/ , shared/ , _scratch/ , _trash/) is denied
with a message pointing at the gateway call to use instead.

This is the *secondary* enforcement layer. The primary one is the runtime gateway
(services/abn_assets.asset_path), which catches every agent regardless of model. This
hook just gives Claude agents a fast, pre-write nudge so they self-correct in the loop.

Wired via .claude/settings.json:
  hooks.PreToolUse[matcher="Write|Edit|NotebookEdit|Bash"] -> this script.

Hook contract: reads a JSON event on stdin, prints a JSON decision on stdout.
  deny  -> {"hookSpecificOutput": {"hookEventName":"PreToolUse",
                                   "permissionDecision":"deny",
                                   "permissionDecisionReason": "..."}}
  allow -> exit 0 with no/empty output (silent pass).
"""
import json
import os
import re
import sys
from pathlib import Path

# Resolve the asset store the same way services/agenticnews.py does, WITHOUT importing
# the app (hooks run bare). Read .env if the var isn't already in the environment.
def _asset_root() -> Path | None:
    val = os.getenv("ABN_ASSETS_DIR")
    if not val:
        # walk up to find .env next to the repo root
        here = Path(__file__).resolve()
        for parent in here.parents:
            envf = parent.parent / ".env" if parent.name == "yt-pipeline" else parent / ".env"
            if envf.exists():
                for line in envf.read_text().splitlines():
                    if line.startswith("ABN_ASSETS_DIR="):
                        val = line.split("=", 1)[1].strip()
                        break
            if val:
                break
    if not val:
        return None
    try:
        return Path(val).expanduser().resolve()
    except OSError:
        return None


# Allowed top-level dirs inside the asset store. A write whose first path component
# under the store isn't one of these (or a real ep_<id> dir) is a "stray root write".
ALLOWED_TOP = {
    "_shared", "_scratch", "_trash", "_published", "release",
    "editor_renders", "editor_timelines", "editor_title_assets", "align",
    "card_backgrounds", "broll_library", "ai-chat-repo", "voice_bakeoff",
}
# A real episode dir: ep_<hex> / rec_ep_<hex> / legacy ep<N>. A write scoped to one of
# these is fine ONLY if it lands in a schema subdir (or is an episode-root singleton) —
# see _EP_SUBDIRS below. A flat file dumped straight at {ep_id}/ root (e.g. a bare
# Path(ASSETS/ep_x/"s0_card.png").write_bytes via a broken/rogue function) is the exact
# off-schema-dump incident this guard exists to block, even though the top dir is valid.
_EP_TOP_RE = re.compile(r"^(?:rec_)?ep_[0-9a-f]{6,}$|^ep\d+$")

# Inside a {ep_id}/ dir the only sanctioned destinations are the typed layer-source subdirs
# (mirrors abn_assets.KINDS subdirs) ...
_EP_SUBDIRS = {"footage", "css", "remotion", "broll", "audio", "renders", "scratch"}
# ... plus the two episode-root singletons (abn_assets.KINDS '.' kinds).
_EP_ROOT_SINGLETONS = {"timeline.json", "manifest.json"}

# crude path extraction from a bash command (redirects, cp/mv/tee targets, common writers)
_BASH_PATH_RE = re.compile(
    r"""(?:>>?|--output[= ]|-o |\btee\b\s+|\bcp\b\s+\S+\s+|\bmv\b\s+\S+\s+)\s*['"]?([^\s'">|]+)""",
    re.X,
)


def _candidate_paths(tool: str, ti: dict, root: "Path | None" = None) -> list[str]:
    if tool in ("Write", "Edit", "NotebookEdit"):
        p = ti.get("file_path") or ti.get("notebook_path") or ""
        return [p] if p else []
    if tool == "Bash":
        cmd = ti.get("command") or ""
        found = list(_BASH_PATH_RE.findall(cmd))
        # also catch any bare token that points INTO the asset store (positional output
        # args like `ffmpeg -i in.mp4 /…/agenticnews_assets/out.mp4` have no redirect/flag)
        if root is not None:
            rootstr = str(root)
            for tok in re.split(r"\s+", cmd):
                t = tok.strip("'\"")
                if "agenticnews_assets" in t or t.startswith(rootstr):
                    found.append(t)
        return found
    return []


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed event — don't block

    tool = event.get("tool_name") or event.get("toolName") or ""
    ti = event.get("tool_input") or event.get("toolInput") or {}
    root = _asset_root()
    if root is None or tool not in ("Write", "Edit", "NotebookEdit", "Bash"):
        sys.exit(0)

    for raw in _candidate_paths(tool, ti, root):
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = (Path.cwd() / p)
            p = p.resolve()
        except OSError:
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue  # not under the asset store — none of our business
        parts = rel.parts
        # writing a file directly at the store root, or into a non-allowed top dir
        is_ep = bool(parts) and bool(_EP_TOP_RE.match(parts[0]))
        top_ok = bool(parts) and (parts[0] in ALLOWED_TOP or is_ep)
        # Inside a valid {ep_id}/ the write must descend into a schema subdir
        # (footage/css/remotion/broll/audio/renders/scratch), OR be a flat episode-root
        # singleton (timeline.json / manifest.json). A flat file at {ep_id}/<other-name>
        # is the off-schema dump a bare Path(ASSETS/ep_x/"s0_card.png").write_bytes()
        # produces when it bypasses the gateway — block it even though the top dir is valid.
        ep_dump = (
            is_ep and len(parts) >= 2
            and parts[1] not in _EP_SUBDIRS
            and not (len(parts) == 2 and parts[1] in _EP_ROOT_SINGLETONS)
        )
        if len(parts) <= 1 or not top_ok or ep_dump:
            reason = (
                f"Blocked: '{rel}' is a stray write into the ABN asset store "
                f"({root}).\n"
                f"Episode assets must be scoped — use the gateway:\n"
                f"    from services.abn_assets import asset_path\n"
                f"    asset_path(ep_id, kind, slug)   # e.g. asset_path('ep_648e806a','card','s0')\n"
                f"Shared assets: shared_path('audio'|'brand'|'broll_library'|'card_backgrounds', name).\n"
                f"Throwaway test files: write under episodes/<ep>/scratch/ or _scratch/.\n"
                f"See yt-pipeline/drafts/EPISODE-FOLDER-SOP.md."
            )
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }))
            sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
