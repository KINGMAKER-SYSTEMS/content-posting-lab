"""Login account values must never become executable subprocess source."""

import asyncio
import builtins
import sys
from types import ModuleType, SimpleNamespace

import pytest

from routers import upload


@pytest.mark.parametrize("account", [
    "ordinary.account_123",
    "account'with-quote",
    "account\\with-backslash",
    "account\nwith-newline",
    "account-é",
    "account' + (__import__('builtins')._login_injection_probe() or '') + '",
])
def test_login_preserves_account_as_data_without_execution(monkeypatch, account):
    commands = []

    async def fake_exec(*args, **kwargs):
        commands.append(args)
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(upload.asyncio, "create_subprocess_exec", fake_exec)
    response = asyncio.run(upload.trigger_login(account))
    command, = commands
    assert command[:2] == (sys.executable, "-c")

    calls = []
    injections = []
    uploader = ModuleType("tiktokautouploader")
    uploader.upload_tiktok = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, "tiktokautouploader", uploader)
    monkeypatch.setattr(sys, "argv", ["-c", *command[3:]])
    monkeypatch.setattr(builtins, "_login_injection_probe",
                        lambda: injections.append(True), raising=False)

    # Execute only the captured static runner with the uploader replaced by a
    # recorder. No subprocess, browser, provider or network call is performed.
    exec(command[2], {})

    assert injections == []
    assert calls == [{"video": "__login_trigger__", "description": "",
                      "accountname": account, "headless": False, "stealth": True}]
    assert command[3:] == (account,)
    assert account not in command[2]
    assert response["account"] == account
    assert response["pid"] == 4242
