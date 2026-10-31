from pathlib import Path

import pytest

from services import scrambler_feed_pull as pull


def test_independent_switch_and_no_calls_when_off(monkeypatch):
    calls=[]
    monkeypatch.setattr(pull,"run_summary_pull_once",lambda: calls.append("summary"))
    monkeypatch.setenv("SCRAMBLER_ENABLED","0")
    monkeypatch.delenv("CONTENT_LAB_ORIGINALITY_SUMMARY_FEED_ENABLED",raising=False)
    pull.maybe_pull_summary_once(); assert calls==[]
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_SUMMARY_FEED_ENABLED","1")
    pull.maybe_pull_summary_once(); assert calls==["summary"]



@pytest.mark.parametrize("scrambler,summary,expected", [("0","0",[]),("0","1",["summary"]),("1","0",["scrambler"]),("1","1",["scrambler","summary"])])
def test_actual_scheduler_cross_flags(monkeypatch,scrambler,summary,expected):
    calls=[]
    class Once:
        def wait(self,seconds): return True
    monkeypatch.setattr(pull,"_FEED_PULL_STOP",Once())
    monkeypatch.setattr(pull,"run_feed_pull_once",lambda:calls.append("scrambler"))
    monkeypatch.setattr(pull,"run_summary_pull_once",lambda:calls.append("summary"))
    monkeypatch.setenv("SCRAMBLER_ENABLED",scrambler)
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_SUMMARY_FEED_ENABLED",summary)
    pull._feed_pull_loop()
    assert calls==expected


def test_existing_lifespan_starts_and_stops_shared_scheduler():
    app=(Path(pull.__file__).resolve().parents[1]/"app.py").read_text()
    assert "start_feed_pull_scheduler()" in app
    assert "stop_feed_pull_scheduler()" in app
