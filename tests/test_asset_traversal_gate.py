"""Asset-gateway traversal guard: a /agenticnews-assets/ src must never resolve to,
or be persisted pointing at, a file outside the managed asset tree (symlink hop or ../).

Covers all three chokepoints:
  - openshot_bridge.resolve_managed_asset / _resolve_asset_src (render-resolution)
  - abn_factory._resolve_asset                                  (render-resolution)
  - editor_timeline._asset_from_payload (asset.import)          (upload-time persist)
"""
import os
from pathlib import Path

import pytest

from services import openshot_bridge
from services import editor_timeline


def test_resolve_managed_asset_accepts_in_tree(tmp_path):
    (tmp_path / "_shared" / "music").mkdir(parents=True)
    f = tmp_path / "_shared" / "music" / "bed.mp3"
    f.write_bytes(b"x")
    out = openshot_bridge.resolve_managed_asset("_shared/music/bed.mp3", tmp_path)
    assert out == Path(os.path.normpath(str(f)))


def test_resolve_managed_asset_rejects_dotdot_escape(tmp_path):
    with pytest.raises(openshot_bridge.AssetTraversalError):
        openshot_bridge.resolve_managed_asset("_shared/../../../../etc/passwd", tmp_path)


def test_resolve_managed_asset_rejects_absolute(tmp_path):
    with pytest.raises(openshot_bridge.AssetTraversalError):
        openshot_bridge.resolve_managed_asset("/etc/passwd", tmp_path)


def test_resolve_managed_asset_rejects_bare_dotdot(tmp_path):
    with pytest.raises(openshot_bridge.AssetTraversalError):
        openshot_bridge.resolve_managed_asset("../sibling/secret.txt", tmp_path)


def test_resolve_managed_asset_follows_trusted_in_store_symlink(tmp_path):
    # Back-compat migration symlinks (flat name -> a DIFFERENT physical store) are sanctioned:
    # the lexical gate must NOT pre-resolve them. The legacy flat name stays under the root
    # lexically even though its realpath target lives on another volume.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = elsewhere / "bed.mp3"
    real.write_bytes(b"x")
    root = tmp_path / "store"
    root.mkdir()
    (root / "bed.mp3").symlink_to(real)  # gateway-placed back-compat link
    out = openshot_bridge.resolve_managed_asset("bed.mp3", root)
    assert out == Path(os.path.normpath(str(root / "bed.mp3")))
    assert out.exists()  # OS follows the trusted symlink at read time


def test_resolve_managed_asset_strips_cachebuster(tmp_path):
    f = tmp_path / "episode.mp4"
    f.write_bytes(b"x")
    out = openshot_bridge.resolve_managed_asset("episode.mp4?rev=3", tmp_path)
    assert out == Path(os.path.normpath(str(f)))


def test_resolve_asset_src_blocks_escape(tmp_path):
    with pytest.raises(openshot_bridge.AssetTraversalError):
        openshot_bridge._resolve_asset_src(
            "/agenticnews-assets/_shared/../../../../etc/passwd", asset_root=tmp_path
        )


def test_resolve_asset_src_passthrough_non_gateway(tmp_path):
    # Non-gateway srcs (already-disk paths, http urls) are untouched.
    assert openshot_bridge._resolve_asset_src("/abs/disk/path.mp4", asset_root=tmp_path) == "/abs/disk/path.mp4"
    assert openshot_bridge._resolve_asset_src("https://x/y.mp4", asset_root=tmp_path) == "https://x/y.mp4"


def test_abn_factory_resolve_asset_blocks_escape(monkeypatch, tmp_path):
    import services.abn_factory as factory

    monkeypatch.setattr(factory, "ASSETS", tmp_path)
    with pytest.raises(openshot_bridge.AssetTraversalError):
        factory._resolve_asset("/agenticnews-assets/_shared/../../../../etc/passwd")


def test_asset_import_rejects_traversing_src(monkeypatch, tmp_path):
    import services.agenticnews as db

    monkeypatch.setattr(db, "ASSETS_DIR", tmp_path)
    project = editor_timeline.new_project("ep_real", width=1920, height=1080, fps=30)
    cmd = {
        "id": "cmd1",
        "op": "asset.import",
        "expectedRevision": project.get("revision", 0),
        "payload": {
            "assetId": "evil",
            "type": "image",
            "src": "/agenticnews-assets/_shared/../../../../etc/passwd",
        },
    }
    with pytest.raises(editor_timeline.CommandValidationError):
        editor_timeline.apply_command(project, cmd)


def test_asset_import_accepts_in_tree_src(monkeypatch, tmp_path):
    import services.agenticnews as db

    monkeypatch.setattr(db, "ASSETS_DIR", tmp_path)
    project = editor_timeline.new_project("ep_real", width=1920, height=1080, fps=30)
    cmd = {
        "id": "cmd1",
        "op": "asset.import",
        "expectedRevision": project.get("revision", 0),
        "payload": {
            "assetId": "ok",
            "type": "image",
            "src": "/agenticnews-assets/_shared/cards/title.png",
        },
    }
    result = editor_timeline.apply_command(project, cmd)
    assert result["assets"]["ok"]["src"] == "/agenticnews-assets/_shared/cards/title.png"
