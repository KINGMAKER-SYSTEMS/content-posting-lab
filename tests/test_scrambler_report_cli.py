"""Actual private CLI subprocesses over temporary durable core ledgers."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from services import scrambler as core

TOOL = Path(__file__).resolve().parents[1] / "tools/scrambler_report.py"
NOW = "2026-10-29T12:01:00Z"


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("SCRAMBLER_"):
            monkeypatch.delenv(key, raising=False)


def ledger(tmp_path, monkeypatch, page="used", count=1):
    path = tmp_path / "history.json"
    choices = [core.ScramblerDraw("none", 0.3, 5000, 0),
               core.ScramblerDraw("camera", 0.35, 5500, 100)]
    for index in range(count):
        monkeypatch.setattr(core, "_candidate", lambda *a, i=index, **k: choices[i])
        job = f"job-{index}"
        core.reserve_job_cuts(page, job, 1, path=path, now="2026-10-29T12:00:00Z")
        core.mark_cut_rendered(page, job, 0, path=path,
            asset_sha256="a" * 64, rendered_at="2026-10-29T12:00:00Z")
    return path


def run(path, *args, env=None):
    history_args = ["--history", str(path)] if path is not None else []
    process = subprocess.run([sys.executable, str(TOOL), *history_args,
        "--now", NOW, *args], env={**os.environ, **(env or {})},
        text=True, capture_output=True, timeout=30)
    assert not process.stderr, process.stderr
    return process.returncode, json.loads(process.stdout)


def test_explicit_historical_page_is_reported_even_off(tmp_path, monkeypatch):
    path = ledger(tmp_path, monkeypatch)
    before = (path.read_bytes(), core._created_marker_path(path).read_bytes())
    code, result = run(path, "--page", "used", env={"SCRAMBLER_ENABLED": "0"})
    assert code == 0 and result["alerts"] == []
    assert result["schema"] == "content-lab.scrambler-daily-report.v1"
    assert result["scope"] == "lab_rendered_last_24h"
    assert result["pages"]["used"]["cuts"] == 1
    assert before == (path.read_bytes(), core._created_marker_path(path).read_bytes())


def test_configured_active_zero_page_is_included_and_denied_page_skipped(tmp_path, monkeypatch):
    path = ledger(tmp_path, monkeypatch)
    code, result = run(path, env={"SCRAMBLER_ENABLED": "1",
        "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT": "1",
        "SCRAMBLER_PAGES": "used,zero,denied", "SCRAMBLER_DENY_PAGES": "denied"})
    assert code == 1
    assert set(result["pages"]) == {"used", "zero"}
    assert result["pages"]["zero"]["cuts"] == 0
    assert result["alerts"] == [{"kind": "zero", "pageId": "zero"}]


def test_repeated_explicit_pages_and_old_history_zero_alert(tmp_path, monkeypatch):
    path = ledger(tmp_path, monkeypatch)
    code, result = run(path, "--page", "used", "--page", "other", "--page", "other",
        "--now", "2026-10-31T12:01:00Z")
    assert code == 1 and set(result["pages"]) == {"used", "other"}
    assert all(page["cuts"] == 0 for page in result["pages"].values())
    assert {alert["pageId"] for alert in result["alerts"]} == {"used", "other"}
    assert all(alert["kind"] == "zero" for alert in result["alerts"])


def test_schema_valid_repeats_are_alerted_from_actual_durable_rows(tmp_path, monkeypatch):
    path = ledger(tmp_path, monkeypatch, count=2)
    history = json.loads(path.read_text())
    first, second = history["pages"]["used"]
    # Model schema-valid historical duplicate content, retaining distinct real
    # operation identities and actual render order; report must expose it.
    second.update(core._draw_from_row(first).manifest())
    core._save_history_at(path, history)
    code, result = run(path, "--page", "used")
    assert code == 1 and result["alerts"] == [{"kind": "repeat", "pageId": "used"}]
    report = result["pages"]["used"]
    assert report["cuts"] == 2 and len(report["repeatCombos"]) == 1
    assert report["consecutiveLengthRepeats"] == report["consecutiveTreatmentRepeats"] == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt", "missing_marker", "bad_marker", "mismatched_marker"])
def test_unavailable_history_never_bootstraps_or_resets(tmp_path, monkeypatch, damage):
    path = tmp_path / "absent" / "history.json" if damage == "missing" else ledger(tmp_path, monkeypatch)
    marker = core._created_marker_path(path)
    if damage == "corrupt": path.write_text("{broken")
    elif damage == "missing_marker": marker.unlink()
    elif damage == "bad_marker": marker.write_text("{}")
    elif damage == "mismatched_marker":
        value = json.loads(marker.read_text())
        value["history"] = "another-ledger.json"
        marker.write_text(json.dumps(value))
    before = {str(p): p.read_bytes() for p in (path, marker) if p.exists()}
    code, result = run(path, "--page", "used")
    assert code == 2 and result["pages"] == {}
    assert result["alerts"][0]["kind"] == "unavailable"
    assert before == {str(p): p.read_bytes() for p in (path, marker) if p.exists()}
    if damage == "missing": assert not path.parent.exists()


def test_implicit_disabled_pages_skip_missing_history_without_false_zero(tmp_path):
    path = tmp_path / "missing" / "history.json"
    code, result = run(path, env={"SCRAMBLER_PAGES": "used", "SCRAMBLER_ENABLED": "0"})
    assert code == 0 and result["pages"] == {} and result["alerts"] == []
    assert not path.parent.exists()


def test_implicit_missing_compatibility_is_unavailable_without_history_io(tmp_path):
    path = tmp_path / "missing" / "history.json"
    code, result = run(path, env={"SCRAMBLER_PAGES": "used", "SCRAMBLER_ENABLED": "1"})
    assert code == 2 and result["alerts"] == [{"kind": "unavailable", "pageId": "used",
        "reason": "scrambler_worker_source_window_compat_required"}]
    assert not path.parent.exists()


def test_invalid_report_clock_refuses_without_history_io(tmp_path):
    path = tmp_path / "missing" / "history.json"
    code, result = run(path, "--page", "used", "--now", "not-a-clock")
    assert code == 2 and result["alerts"][0]["kind"] == "unavailable"
    assert not path.parent.exists()


def test_environment_durable_history_path_is_used(tmp_path, monkeypatch):
    path = ledger(tmp_path, monkeypatch)
    code, result = run(None, "--page", "used", env={"SCRAMBLER_HISTORY_PATH": str(path)})
    assert code == 0 and result["pages"]["used"]["cuts"] == 1
