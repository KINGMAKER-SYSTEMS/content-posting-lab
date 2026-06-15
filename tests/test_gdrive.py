"""Tests for services.gdrive specific-exception handling.

The Drive helpers used to wrap every API/credential call in a bare
``except Exception`` that silently returned a sentinel (None/False/0).
That masked real failures — including KeyboardInterrupt and other
system-level errors. These tests pin the new behavior:

  * expected Drive API failures (googleapiclient Error) -> sentinel return
  * expected I/O failures (OSError) -> sentinel return
  * everything else (e.g. KeyboardInterrupt) propagates
"""

import base64
import json

import pytest
from googleapiclient.errors import Error as GoogleApiError

import services.gdrive as gdrive


class _FakeExecutable:
    """Stands in for a googleapiclient request object whose .execute() raises."""

    def __init__(self, exc):
        self._exc = exc

    def execute(self):
        raise self._exc


class _FakeFiles:
    def __init__(self, exc):
        self._exc = exc

    def get(self, **_kwargs):
        return _FakeExecutable(self._exc)

    def create(self, **_kwargs):
        return _FakeExecutable(self._exc)

    def delete(self, **_kwargs):
        return _FakeExecutable(self._exc)


class _FakeService:
    def __init__(self, exc):
        self._exc = exc

    def files(self):
        return _FakeFiles(self._exc)


def _patch_service(monkeypatch, exc):
    monkeypatch.setattr(gdrive, "get_service", lambda: _FakeService(exc))


# --- API-wrapping helpers: expected errors are swallowed -------------------


@pytest.mark.parametrize("exc", [GoogleApiError("boom"), OSError("socket down")])
def test_get_folder_info_swallows_expected(monkeypatch, exc):
    _patch_service(monkeypatch, exc)
    assert gdrive.get_folder_info("fid") is None


@pytest.mark.parametrize("exc", [GoogleApiError("boom"), OSError("socket down")])
def test_create_folder_swallows_expected(monkeypatch, exc):
    _patch_service(monkeypatch, exc)
    assert gdrive.create_folder("name") is None


@pytest.mark.parametrize("exc", [GoogleApiError("boom"), OSError("socket down")])
def test_delete_file_swallows_expected(monkeypatch, exc):
    _patch_service(monkeypatch, exc)
    assert gdrive.delete_file("fid") is False


# --- The core fix: non-API errors now propagate ----------------------------


def test_get_folder_info_propagates_keyboardinterrupt(monkeypatch):
    _patch_service(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        gdrive.get_folder_info("fid")


def test_delete_file_propagates_keyboardinterrupt(monkeypatch):
    _patch_service(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        gdrive.delete_file("fid")


def test_create_folder_propagates_runtimeerror(monkeypatch):
    # A genuine bug (not a Drive/IO failure) must not be masked as None.
    _patch_service(monkeypatch, RuntimeError("real bug"))
    with pytest.raises(RuntimeError):
        gdrive.create_folder("name")


# --- Credential loading: bad JSON -> None, KeyboardInterrupt propagates -----


def test_get_credentials_bad_base64_json_returns_none(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", base64.b64encode(b"not json").decode())
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    assert gdrive._get_credentials() is None


def test_get_credentials_propagates_keyboardinterrupt(monkeypatch):
    good_b64 = base64.b64encode(json.dumps({"type": "service_account"}).encode()).decode()
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", good_b64)

    def _boom(*_a, **_k):
        raise KeyboardInterrupt()

    # from_service_account_info is the call that would normally fail on bad
    # creds; make it raise KeyboardInterrupt to prove it is no longer swallowed.
    monkeypatch.setattr(
        gdrive.Credentials, "from_service_account_info", staticmethod(_boom)
    )
    with pytest.raises(KeyboardInterrupt):
        gdrive._get_credentials()
