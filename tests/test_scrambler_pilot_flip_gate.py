"""The pilot-flip gate: ``--pull-feed-once`` and ``--preflight --as-enabled``.

The pilot plan deploys with SCRAMBLER_ENABLED off and flips only after a gate
passes. These tests pin that the gate proves something while the switch is off:

* ``tools/scrambler_report.py --pull-feed-once`` runs ONE real-path pull
  (``run_feed_pull_once`` -> ``r2_fetch`` -> ``services.r2.client().get_object``)
  regardless of the switch, prints one JSON line naming only the error class,
  reason name, feed file and R2 error Code, and exits 0/3/4/5/6;
* ``--preflight --as-enabled`` evaluates every ON-path requirement as if the
  switch were 1, plus proof of one verified pull, without changing the process
  environment; plain ``--preflight`` is unchanged;
* the pulled data files carry the manifest's ``generated_at`` as their mtime,
  so a stale producer feed fails the gate (Tides MED-F).

R2 is a fake client raising real ``botocore.exceptions.ClientError`` objects;
nothing here reaches the network.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from services import r2
from services import scrambler_feed_pull as sfp
from services import scrambler_originality as so
from services import scrambler_preflight as pf
from services.scrambler_feed_pull import ScramblerFeedPullError, pull_once, sha256_hex
from tools import scrambler_report as report

PILOT_PAGES = "acct:trailheadtravis,acct:backpack-bohemian,acct:freddy-2am"
BUCKET = "rt-content-buckets"
PREFIX = "originality/feed/"
SECRET_ENDPOINT = "https://SENTINEL-ACCOUNT-0f0f.r2.cloudflarestorage.com"
SECRET_KEY_ID = "SENTINELKEYID0123456789"
SECRET_KEY = "SENTINELSECRET/abcdefghijklmnopqrstuvwxyz"
MASTER = "b" * 64


def _usage(n: int = 3) -> bytes:
    rows = [json.dumps({
        "page": f"@page{i}", "posted_at": "2026-10-28T12:00:00-0400",
        "video_id": str(7_000_000_000_000_000_000 + i), "final_sha": "f" * 64,
        "asset_sha": "a" * 64, "origin_kind": "yt", "origin_id": f"vid{i:02d}",
        "origin_start_s": 0.0, "origin_end_s": 9.0, "master_sha": MASTER,
        "recipe": {"speed": None, "reverse": None, "loop_split": None, "crop": None, "hflip": None},
        "fp": f"fp/finals/{'f' * 64}.fp.json", "prov": "d1", "gaps": [],
    }) for i in range(n)]
    return ("\n".join(rows) + "\n").encode()


def _origins() -> bytes:
    return json.dumps({MASTER: {"page": "page0", "origin_kind": "yt", "origin_id": "vid00",
                                "origin_start_s": 0.0, "origin_end_s": 9.0,
                                "evidence": "manifest", "confidence": "high"}}).encode()


def _stamp(age: timedelta) -> str:
    return (datetime.now(timezone.utc) - age).strftime("%Y-%m-%dT%H:%M:%SZ")


def _manifest(usage: bytes, origins: bytes, *, generated_at: str | None = None,
              usage_sha: str | None = None) -> bytes:
    rows = sum(1 for line in usage.split(b"\n") if line.strip())
    return (json.dumps({"generated_at": generated_at or _stamp(timedelta(0)), "rows": rows,
                        "sha256s": {"usage.jsonl": usage_sha or sha256_hex(usage),
                                    "origins.json": sha256_hex(origins)},
                        "dropped_unknown_origin": 0}) + "\n").encode()


class FakeR2:
    """``get_object`` like boto3's S3 client; errors are real botocore ClientErrors
    whose message carries the endpoint and key id (it must never be echoed)."""

    def __init__(self, objects: dict[str, bytes], errors: dict[str, object] | None = None):
        self.objects = objects
        self.errors = errors or {}
        self.calls: list[tuple[str, str]] = []

    def get_object(self, Bucket, Key):  # noqa: N803 (boto3 keyword names)
        self.calls.append((Bucket, Key))
        error = self.errors.get(Key)
        if isinstance(error, str):
            raise ClientError({"Error": {"Code": error, "Message": (
                f"{error} for {SECRET_KEY_ID} at {SECRET_ENDPOINT}/{Bucket}/{Key}")},
                "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject")
        if isinstance(error, BaseException):
            raise error
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    """The documented pilot env MINUS the switch, fake R2 creds, no feed files yet."""
    for key in tuple(os.environ):
        if key.startswith(("SCRAMBLER_", "CONTENT_LAB_ORIGINALITY_", "RAILWAY_")):
            monkeypatch.delenv(key, raising=False)
    volume = tmp_path / "projects"
    (volume / "originality").mkdir(parents=True)
    env = {
        "SCRAMBLER_PAGES": PILOT_PAGES,
        "SCRAMBLER_FAST_SHARE": "0",
        "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT": "1",
        "CONTENT_LAB_ORIGINALITY_USAGE_PATH": str(volume / "originality" / "usage.jsonl"),
        "SCRAMBLER_FEED_R2_BUCKET": BUCKET,
        "SCRAMBLER_FEED_R2_PREFIX": PREFIX,
        "RAILWAY_VOLUME_MOUNT_PATH": str(volume),
        "SCRAMBLER_HISTORY_PATH": str(volume / "scrambler-history.json"),
        "R2_ENDPOINT": SECRET_ENDPOINT,
        "R2_ACCESS_KEY_ID": SECRET_KEY_ID,
        "R2_SECRET_ACCESS_KEY": SECRET_KEY,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    so._index_cache.clear()
    yield volume / "originality"
    so._index_cache.clear()


def _bucket(usage=None, origins=None, manifest=None, **manifest_kw) -> dict[str, bytes]:
    usage = _usage() if usage is None else usage
    origins = _origins() if origins is None else origins
    manifest = manifest if manifest is not None else _manifest(usage, origins, **manifest_kw)
    return {f"{PREFIX}manifest.json": manifest, f"{PREFIX}usage.jsonl": usage,
            f"{PREFIX}origins.json": origins}


def _install(monkeypatch, fake: FakeR2) -> FakeR2:
    monkeypatch.setattr(r2, "client", lambda: fake)
    return fake


def _cli(capsys, *argv) -> tuple[int, dict, str]:
    code = report.main(list(argv))
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, captured.out
    return code, json.loads(lines[0]), captured.out + captured.err


def _seed_old(dest: Path) -> dict[str, bytes]:
    old = {"usage.jsonl": b"OLD-U\n", "origins.json": b"{}", "manifest.json": b"{}"}
    for name, raw in old.items():
        (dest / name).write_bytes(raw)
        os.utime(dest / name, (1_600_000_000, 1_600_000_000))
    return old


def _assert_untouched(dest: Path, old: dict[str, bytes]) -> None:
    for name, raw in old.items():
        assert (dest / name).read_bytes() == raw, name
        assert (dest / name).stat().st_mtime == 1_600_000_000, name


# ── --pull-feed-once ──────────────────────────────────────────────────────

@pytest.mark.parametrize("switch", [None, "0"])
def test_pull_feed_once_pulls_while_the_switch_is_off(pilot, monkeypatch, capsys, switch):
    if switch is not None:
        monkeypatch.setenv("SCRAMBLER_ENABLED", switch)
    objects = _bucket()
    fake = _install(monkeypatch, FakeR2(objects))
    code, out, _ = _cli(capsys, "--pull-feed-once")
    assert code == 0, out
    manifest = json.loads(objects[f"{PREFIX}manifest.json"])
    assert out == {"schema": "content-lab.scrambler-feed-pull-once.v1", "result": "ok",
                   "generated_at": manifest["generated_at"], "rows": 3, "sha_ok": True,
                   "warnings": []}
    assert fake.calls == [(BUCKET, f"{PREFIX}{name}")
                          for name in ("manifest.json", "usage.jsonl", "origins.json")]
    for name in ("manifest.json", "usage.jsonl", "origins.json"):
        assert (pilot / name).read_bytes() == objects[f"{PREFIX}{name}"]
    assert os.environ.get("SCRAMBLER_ENABLED") == switch  # never flipped by the tool


def test_pull_feed_once_access_denied_names_only_the_code(pilot, monkeypatch, capsys):
    old = _seed_old(pilot)
    _install(monkeypatch, FakeR2(_bucket(), {f"{PREFIX}manifest.json": "AccessDenied"}))
    code, out, text = _cli(capsys, "--pull-feed-once")
    assert code == report.PULL_EXIT_ACCESS_DENIED == 3
    assert out == {"schema": "content-lab.scrambler-feed-pull-once.v1", "result": "error",
                   "error_class": "r2_access_denied", "reason": "fetch_failed",
                   "file": "manifest.json", "r2_code": "AccessDenied",
                   "exception": "ClientError"}
    for secret in (SECRET_ENDPOINT, SECRET_KEY_ID, SECRET_KEY, "SENTINEL", BUCKET, str(pilot)):
        assert secret not in text
    _assert_untouched(pilot, old)


@pytest.mark.parametrize("code_name", ["InvalidAccessKeyId", "SignatureDoesNotMatch"])
def test_pull_feed_once_bad_credentials_are_access_denied(pilot, monkeypatch, capsys, code_name):
    _install(monkeypatch, FakeR2(_bucket(), {f"{PREFIX}manifest.json": code_name}))
    code, out, _ = _cli(capsys, "--pull-feed-once")
    assert (code, out["error_class"], out["r2_code"]) == (3, "r2_access_denied", code_name)


@pytest.mark.parametrize(("missing", "error_class"), [
    ("manifest.json", "manifest_missing"),
    ("usage.jsonl", "feed_file_missing"),
    ("origins.json", "feed_file_missing"),
])
def test_pull_feed_once_missing_object_is_the_producer(pilot, monkeypatch, capsys,
                                                       missing, error_class):
    old = _seed_old(pilot)
    _install(monkeypatch, FakeR2(_bucket(), {f"{PREFIX}{missing}": "NoSuchKey"}))
    code, out, text = _cli(capsys, "--pull-feed-once")
    assert code == report.PULL_EXIT_FEED_MISSING == 4
    assert (out["error_class"], out["file"], out["r2_code"]) == (error_class, missing, "NoSuchKey")
    assert "SENTINEL" not in text
    _assert_untouched(pilot, old)


@pytest.mark.parametrize(("bucket_kw", "reason", "file"), [
    ({"usage_sha": "c" * 64}, "sha256_mismatch", "usage.jsonl"),
    ({"manifest": b"not json"}, "manifest_invalid", None),
    ({"manifest": b'{"generated_at": "2026-10-29T06:00:00Z", "rows": 3}'}, "manifest_invalid", None),
    ({"generated_at": "last tuesday"}, "manifest_invalid", None),
])
def test_pull_feed_once_invalid_feed_fails_closed(pilot, monkeypatch, capsys,
                                                  bucket_kw, reason, file):
    old = _seed_old(pilot)
    _install(monkeypatch, FakeR2(_bucket(**bucket_kw)))
    code, out, _ = _cli(capsys, "--pull-feed-once")
    assert code == report.PULL_EXIT_FEED_INVALID == 5
    assert (out["result"], out["error_class"], out["reason"], out["file"]) == (
        "error", "feed_invalid", reason, file)
    _assert_untouched(pilot, old)


def test_pull_feed_once_row_count_mismatch_fails_closed(pilot, monkeypatch, capsys):
    old = _seed_old(pilot)
    usage, origins = _usage(3), _origins()
    manifest = json.loads(_manifest(usage, origins))
    manifest["rows"] = 4
    _install(monkeypatch, FakeR2(_bucket(usage, origins, json.dumps(manifest).encode())))
    code, out, _ = _cli(capsys, "--pull-feed-once")
    assert (code, out["reason"]) == (5, "rows_mismatch")
    _assert_untouched(pilot, old)


def test_pull_feed_once_no_such_bucket_is_other(pilot, monkeypatch, capsys):
    _install(monkeypatch, FakeR2(_bucket(), {f"{PREFIX}manifest.json": "NoSuchBucket"}))
    code, out, text = _cli(capsys, "--pull-feed-once")
    assert code == report.PULL_EXIT_OTHER == 6
    assert (out["error_class"], out["r2_code"]) == ("other", "NoSuchBucket")
    assert "SENTINEL" not in text


def test_pull_feed_once_other_failures_never_echo_values(pilot, monkeypatch, capsys):
    # A network-style error whose message carries the endpoint.
    _install(monkeypatch, FakeR2(_bucket(), {f"{PREFIX}manifest.json": ConnectionError(
        f"Could not connect to the endpoint URL: {SECRET_ENDPOINT} ({SECRET_KEY_ID})")}))
    code, out, text = _cli(capsys, "--pull-feed-once")
    assert (code, out["error_class"], out["reason"], out["r2_code"], out["exception"]) == (
        6, "other", "fetch_failed", None, "ConnectionError")
    assert "SENTINEL" not in text
    # A write failure whose reason carries the destination path (an env value).
    _install(monkeypatch, FakeR2(_bucket()))
    monkeypatch.setattr(sfp, "_write_all", lambda dest, payloads, data_mtime=None: (_ for _ in ()).throw(
        ScramblerFeedPullError(f"write_failed: cannot create {dest}: Permission denied")))
    code, out, text = _cli(capsys, "--pull-feed-once")
    assert (code, out["reason"]) == (6, "write_failed")
    assert str(pilot) not in text


def test_pull_feed_once_unconfigured_bucket_is_other(pilot, monkeypatch, capsys):
    monkeypatch.delenv("SCRAMBLER_FEED_R2_BUCKET")
    fake = _install(monkeypatch, FakeR2(_bucket()))
    code, out, _ = _cli(capsys, "--pull-feed-once")
    assert (code, out["error_class"], out["reason"]) == (6, "other", "feed_unconfigured")
    assert fake.calls == []


def test_cli_argument_rules(capsys):
    for argv in (["--as-enabled"], ["--pull-feed-once", "--preflight"]):
        with pytest.raises(SystemExit) as exc:
            report.main(argv)
        assert exc.value.code == 2
    capsys.readouterr()


# ── MED-F: the data's age is the files' mtime ────────────────────────────

def test_pulled_files_carry_the_manifest_time_as_mtime(pilot, monkeypatch, capsys):
    generated = _stamp(timedelta(hours=5))
    _install(monkeypatch, FakeR2(_bucket(generated_at=generated)))
    t0 = time.time()
    code, out, _ = _cli(capsys, "--pull-feed-once")
    assert code == 0, out
    expected = datetime.fromisoformat(generated.replace("Z", "+00:00")).timestamp()
    for name in ("usage.jsonl", "origins.json"):
        assert (pilot / name).stat().st_mtime == pytest.approx(expected, abs=1), name
    assert (pilot / "manifest.json").stat().st_mtime >= t0 - 1  # manifest: pull time


def test_future_manifest_time_is_capped_at_now(tmp_path):
    usage, origins = _usage(), _origins()
    ahead = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    objects = {"manifest.json": _manifest(usage, origins, generated_at=ahead),
               "usage.jsonl": usage, "origins.json": origins}
    pull_once(objects.__getitem__, tmp_path)
    for name in ("usage.jsonl", "origins.json"):
        assert (tmp_path / name).stat().st_mtime <= time.time() + 1, name


def test_unparseable_generated_at_is_refused_and_old_files_kept(tmp_path):
    old = _seed_old(tmp_path)
    usage, origins = _usage(), _origins()
    objects = {"manifest.json": _manifest(usage, origins, generated_at="yesterday-ish"),
               "usage.jsonl": usage, "origins.json": origins}
    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(objects.__getitem__, tmp_path)
    assert exc.value.reason == "manifest_invalid: generated_at is not an ISO 8601 time"
    _assert_untouched(tmp_path, old)


@pytest.mark.parametrize(("age", "expected"), [
    # 3 days: the loader's own 26 h mtime gate refuses BOTH data files (MED-F),
    # independently of the as-enabled manifest-age check.
    (timedelta(days=3), ["CONTENT_LAB_ORIGINALITY_USAGE_PATH is stale",
                         "origins map is stale", "pulled feed manifest is stale"]),
    # 21 h: inside the 26 h mtime gate, so only the 20 h manifest-age check fails.
    (timedelta(hours=21), ["pulled feed manifest is stale"]),
])
def test_as_enabled_fails_on_a_stale_producer_feed(pilot, monkeypatch, capsys, age, expected):
    _install(monkeypatch, FakeR2(_bucket(generated_at=_stamp(age))))
    code, out, _ = _cli(capsys, "--pull-feed-once")
    assert code == 0 and out["warnings"] == ["manifest_stale"]  # the pull still writes
    code, out, _ = _cli(capsys, "--preflight", "--as-enabled")
    assert code == 1 and out["ready"] is False
    for text in expected:
        assert any(text in problem for problem in out["problems"]), (text, out["problems"])
    if age < timedelta(hours=26):
        assert not any(" is stale: " in p and "manifest" not in p for p in out["problems"])


# ── --preflight --as-enabled: the gate ───────────────────────────────────

def test_as_enabled_gate_fails_until_the_pull_worked_then_passes(pilot, monkeypatch, capsys):
    assert "SCRAMBLER_ENABLED" not in os.environ
    # Plain preflight is unchanged: with the switch off it can never pass.
    code, out, _ = _cli(capsys, "--preflight")
    assert code == 1 and set(out) == {"schema", "ready", "problems"}
    assert "SCRAMBLER_ENABLED must be 1" in out["problems"]
    # As-enabled: the switch is not a problem, the missing feed is.
    code, out, _ = _cli(capsys, "--preflight", "--as-enabled")
    assert code == 1 and out["asEnabled"] is True and out["ready"] is False
    assert "SCRAMBLER_ENABLED must be 1" not in out["problems"]
    assert any("file is missing" in p for p in out["problems"]), out["problems"]
    assert any("pulled feed manifest is missing" in p for p in out["problems"]), out["problems"]
    # R2 denied: the pull fails and the gate still fails.
    _install(monkeypatch, FakeR2(_bucket(), {f"{PREFIX}manifest.json": "AccessDenied"}))
    assert _cli(capsys, "--pull-feed-once")[0] == 3
    assert _cli(capsys, "--preflight", "--as-enabled")[0] == 1
    # The pull works: the gate passes, with the switch still off.
    _install(monkeypatch, FakeR2(_bucket()))
    assert _cli(capsys, "--pull-feed-once")[0] == 0
    code, out, _ = _cli(capsys, "--preflight", "--as-enabled")
    assert (code, out) == (0, {"schema": "content-lab.scrambler-preflight.v1", "ready": True,
                               "asEnabled": True, "problems": []})
    assert "SCRAMBLER_ENABLED" not in os.environ  # evaluated on a copy only
    # Plain preflight still names only the switch.
    code, out, _ = _cli(capsys, "--preflight")
    assert (code, out["problems"]) == (1, ["SCRAMBLER_ENABLED must be 1"])


def test_as_enabled_still_names_a_missing_setting_after_a_good_pull(pilot, monkeypatch, capsys):
    monkeypatch.delenv("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT")
    _install(monkeypatch, FakeR2(_bucket()))
    assert _cli(capsys, "--pull-feed-once")[0] == 0
    code, out, _ = _cli(capsys, "--preflight", "--as-enabled")
    assert code == 1
    assert out["problems"] == ["SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT must be 1"]


def test_as_enabled_requires_proof_of_a_pull_not_just_files(pilot, monkeypatch):
    # Fresh, loadable files copied by hand (no manifest): ON-ready, but no pull proven.
    (pilot / "usage.jsonl").write_bytes(_usage())
    (pilot / "origins.json").write_bytes(_origins())
    env = dict(os.environ, SCRAMBLER_ENABLED="1")
    assert pf.preflight_problems(env) == []
    problems = pf.preflight_problems_as_enabled()
    assert len(problems) == 1 and "pulled feed manifest is missing" in problems[0], problems


def test_as_enabled_names_a_file_changed_after_the_pull(pilot, monkeypatch, capsys):
    _install(monkeypatch, FakeR2(_bucket()))
    assert _cli(capsys, "--pull-feed-once")[0] == 0
    with (pilot / "usage.jsonl").open("ab") as handle:
        handle.write(_usage(1))
    problems = pf.preflight_problems_as_enabled()
    assert any("does not match its manifest sha256" in p and "usage.jsonl" in p
               for p in problems), problems


def test_startup_line_is_unchanged_by_the_new_modes(pilot, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="scrambler.preflight")
    problems = pf.log_startup_preflight()
    assert problems and problems[0] == "SCRAMBLER_ENABLED must be 1"
    assert "scrambler preflight: NOT READY" in caplog.text
    assert not any("pulled feed" in p for p in problems)
