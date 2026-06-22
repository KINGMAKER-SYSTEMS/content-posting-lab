"""Tests for cookie validation in services.upload.

Verifies that list_cookie_files() and get_cookie_status() agree (shared
_load_cookie_file logic) across valid / expired / corrupt / missing cases.
"""
import json
import time

import pytest

from services import upload


def _write_cookie(dirpath, account, data, *, raw=None):
    path = dirpath / f"TK_cookies_{account}.json"
    if raw is not None:
        path.write_text(raw)
    else:
        path.write_text(json.dumps(data))
    return path


@pytest.fixture
def cookie_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "COOKIE_DIR", str(tmp_path))
    return tmp_path


def test_valid_cookie(cookie_dir):
    _write_cookie(cookie_dir, "good", [{"name": "x", "expiry": time.time() + 9999}])
    assert upload.get_cookie_status("good") == "valid"
    files = {f["account"]: f for f in upload.list_cookie_files()}
    assert files["good"]["status"] == "valid"
    assert files["good"]["cookie_count"] == 1
    assert files["good"]["modified"] is not None


def test_expired_cookie(cookie_dir):
    _write_cookie(cookie_dir, "old", [{"name": "x", "expires": time.time() - 1}])
    assert upload.get_cookie_status("old") == "expired"
    files = {f["account"]: f for f in upload.list_cookie_files()}
    assert files["old"]["status"] == "expired"


def test_corrupt_cookie(cookie_dir):
    _write_cookie(cookie_dir, "bad", None, raw="{not json")
    assert upload.get_cookie_status("bad") == "corrupt"
    files = {f["account"]: f for f in upload.list_cookie_files()}
    assert files["bad"]["status"] == "corrupt"
    assert files["bad"]["modified"] is None
    assert files["bad"]["cookie_count"] == 0


def test_missing_cookie(cookie_dir):
    assert upload.get_cookie_status("nope") == "missing"


def test_status_agrees_between_functions(cookie_dir):
    _write_cookie(cookie_dir, "good", [{"name": "x", "expiry": time.time() + 9999}])
    _write_cookie(cookie_dir, "old", [{"expiry": time.time() - 1}])
    _write_cookie(cookie_dir, "bad", None, raw="oops")
    for f in upload.list_cookie_files():
        assert f["status"] == upload.get_cookie_status(f["account"])
