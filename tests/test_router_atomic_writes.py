"""Regression tests: router config-file writes must be crash-safe (fsync'd).

Several routers used to write JSON config/metadata via a raw ``write_text`` (or a
tmp+replace without ``fsync``). A process crash mid-write could leave corrupted
JSON — the exact P0 that hit the upload-job queue. These helpers now route every
write through ``services.json_store.atomic_save`` (tmp + flush + fsync + replace).

Each test asserts the helper actually invokes ``os.fsync`` (proving it goes
through the atomic path, not a bare write), leaves no ``*.tmp`` sidecar, and
produces a complete, re-loadable document. If anyone reverts a helper to a raw
``write_text``, the fsync assertion fails.
"""

import ast
import json
from pathlib import Path

import pytest

from services import json_store


@pytest.fixture
def fsync_spy(monkeypatch):
    """Count os.fsync calls made through json_store during a save."""
    calls = {"n": 0}
    real_fsync = json_store.os.fsync

    def counting_fsync(fd):
        calls["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(json_store.os, "fsync", counting_fsync)
    return calls


def test_burn_save_batch_meta_is_fsynced(tmp_path, fsync_spy):
    from routers import burn

    meta = {"batch_id": "proj-06151200-1", "items": [1, 2, 3]}
    burn._save_batch_meta(tmp_path, meta)

    out = tmp_path / "batch_meta.json"
    assert fsync_spy["n"] >= 1, "batch meta write was not fsync'd (raw write?)"
    assert json.loads(out.read_text(encoding="utf-8")) == meta
    assert not list(tmp_path.glob("*.tmp")), "tmp sidecar leaked"


def test_video_save_prompt_is_fsynced(tmp_path, fsync_spy, monkeypatch):
    from routers import video

    proj_dir = tmp_path / "proj"
    monkeypatch.setattr(video, "PROJECTS_DIR", tmp_path)

    video._save_prompt("proj", {"prompt": "a cat", "provider": "grok"})

    out = proj_dir / "prompts.json"
    assert fsync_spy["n"] >= 1, "prompt write was not fsync'd (raw write?)"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data[0]["prompt"] == "a cat"
    assert not list(proj_dir.glob("*.tmp")), "tmp sidecar leaked"


def test_video_save_jobs_is_fsynced(tmp_path, fsync_spy, monkeypatch):
    from routers import video

    proj_dir = tmp_path / "proj"
    monkeypatch.setattr(video, "PROJECTS_DIR", tmp_path)
    monkeypatch.setitem(video.jobs, "job-1", {"project": "proj", "videos": []})

    try:
        video._save_jobs("proj")
    finally:
        video.jobs.pop("job-1", None)

    out = proj_dir / "jobs.json"
    assert fsync_spy["n"] >= 1, "jobs write was not fsync'd (raw write?)"
    assert "job-1" in json.loads(out.read_text(encoding="utf-8"))
    assert not list(proj_dir.glob("*.tmp")), "tmp sidecar leaked"


# ---------------------------------------------------------------------------
# telegram_config.json gateway guard
# ---------------------------------------------------------------------------
# services/telegram.py is the ONLY sanctioned writer of telegram_config.json:
# every mutation goes through its setters, which run inside mutate_config() (the
# locked load-modify-save) -> save_config() -> atomic_save(). A router that
# reaches around the service to write the config directly — a raw
# open(CONFIG_PATH, "w") + json.dump, an inline atomic_save(CONFIG_PATH), or a
# *bare* save_config() outside the lock — reopens the upload-job-style truncation
# P0 and/or the TOCTOU race that mutate_config()'s lock exists to close.
#
# NOTE: importing json_store.atomic_save for a router's OWN files (burn batch
# meta, video jobs/prompts, slideshow formats) is the CORRECT, fsync'd path —
# pinned by the tests above. So this guard does not ban atomic_save wholesale; it
# bans only the config-specific writer primitives (CONFIG_PATH handle, the
# config-file literal, and bare save_config) in routers/telegram.py, the router
# this ticket put in scope.


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_save_config_routes_through_atomic_save(tmp_path, fsync_spy, monkeypatch):
    """services.telegram.save_config must go through the fsync'd atomic path."""
    from services import telegram as tg

    cfg = tmp_path / "telegram_config.json"
    monkeypatch.setattr(tg, "CONFIG_PATH", cfg)

    tg.save_config(tg._empty_config())

    assert fsync_spy["n"] >= 1, "save_config did not fsync (raw write?)"
    assert json.loads(cfg.read_text(encoding="utf-8"))["version"] == 1
    assert not list(tmp_path.glob("*.tmp")), "tmp sidecar leaked"


def test_telegram_router_does_not_write_config_out_of_band():
    """routers/telegram.py must never write telegram_config.json out-of-band.

    Parses the router with the AST and fails if it imports the config writer
    primitives — ``save_config`` (a bare save reopens the TOCTOU window the lock
    closes) or ``CONFIG_PATH`` (a file handle inviting a raw open/dump) — or
    references the ``telegram_config.json`` literal directly. The sanctioned path
    is the locked setters / ``mutate_config`` (load-modify-save under the lock);
    read-only ``load_config`` is fine.
    """
    py = _project_root() / "routers" / "telegram.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    forbidden = {"save_config", "CONFIG_PATH"}
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "services.telegram",
            "services.json_store",
        ):
            for alias in node.names:
                if alias.name in forbidden:
                    offenders.append(f"imports {alias.name} from {node.module}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.endswith("telegram_config.json"):
                offenders.append(f"references literal {node.value!r}")

    assert not offenders, (
        "routers/telegram.py writes telegram_config.json out-of-band — route "
        "config mutations through the services.telegram setters / mutate_config "
        "(locked load-modify-save), never a bare save_config / raw open:\n  "
        + "\n  ".join(offenders)
    )
