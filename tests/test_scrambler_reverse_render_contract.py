"""Reverse limits with real persisted rendered credits and forced eligible gates."""
from datetime import datetime, timedelta, timezone
import os
import pytest
from services import scrambler as s

DAY = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def force_eligible_gate(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("SCRAMBLER_"):
            monkeypatch.delenv(key, raising=False)
    original = s._unit
    monkeypatch.setattr(s, "_unit", lambda seed, part:
        0.0 if part == "reverse-gate" else original(seed, part))


def commit(path, page, job, drawn, rendered=None):
    draw = s.reserve_job_cuts(page, job, 1, now=drawn.isoformat(), path=path)[0]
    s.mark_cut_rendered(page, job, 0, asset_sha256="a" * 64,
        rendered_at=(rendered or drawn).isoformat(), path=path)
    return draw


def test_reverse_actually_draws_with_rendered_support_and_one_per_day(tmp_path):
    path, page = tmp_path / "history.json", "acct:reverse-positive"
    reverse_count = 0
    for i in range(45):
        draw = commit(path, page, f"same-day-{i}", DAY)
        reverse_count += draw.treatment == "reverse"
        assert reverse_count <= (i + 1) // 15
    assert reverse_count == 1, "forced eligible gates must produce one real reverse, capped per day"
    for day in (1, 2):
        if day == 2:
            filler = commit(path, page, "between-days", DAY + timedelta(days=1))
            assert filler.treatment != "reverse"  # the day-1 reverse must not repeat
        draw = commit(path, page, f"next-day-{day}", DAY + timedelta(days=day))
        assert draw.treatment == "reverse", "rendered support exists and the next ET day has a free reverse"
        reverse_count += 1
    assert reverse_count == 3


def queued_day_two_reverse_before_delayed_day_one_commit(path, page):
    """Build real frozen reservations before the competing actual-day commit."""
    for i in range(14):
        assert commit(path, page, f"support-{i}", DAY).treatment != "reverse"
    late = s.reserve_job_cuts(page, "late", 1, now=DAY.isoformat(), path=path)[0]
    assert late.treatment == "reverse"
    next_day = DAY + timedelta(days=1)
    support = 0
    for attempt in range(60):
        job = f"while-late-pending-{attempt}"
        draw = s.reserve_job_cuts(page, job, 1, now=DAY.isoformat(), path=path)[0]
        assert draw.treatment != "reverse"  # pending late holds the draw day
        try:
            s.assert_cut_renderable(page, job, 0, rendered_at=next_day.isoformat(), path=path)
        except s.ScramblerExhausted:
            s.release_job_reservations(page, job, reason="frozen_sequence_conflict", path=path)
            continue
        s.mark_cut_rendered(page, job, 0, asset_sha256="c"*64,
            rendered_at=next_day.isoformat(), path=path)
        support += 1
        if support >= 15 and draw.length_ms != late.length_ms:
            break
    assert support >= 15
    # Day2 still has NO actual reverse yet. Both reverse reservations are
    # supported by immutable rendered credits; B is frozen before A commits.
    pending = s.reserve_job_cuts(page, "already-queued-day-two", 1,
        now=next_day.isoformat(), path=path)[0]
    assert pending.treatment == "reverse"
    s.mark_cut_rendered(page, "late", 0, asset_sha256="b"*64,
        rendered_at=next_day.isoformat(), path=path)
    # One nonreverse separates actual reverse events, avoiding the independent
    # treatment/length guard masking the specific ET-day refusal under test.
    for attempt in range(30):
        job = f"between-reverses-{attempt}"
        draw = s.reserve_job_cuts(page, job, 1, now=next_day.isoformat(), path=path)[0]
        assert draw.treatment != "reverse"
        try:
            s.assert_cut_renderable(page, job, 0, rendered_at=next_day.isoformat(), path=path)
        except s.ScramblerExhausted:
            s.release_job_reservations(page, job, reason="frozen_sequence_conflict", path=path)
            continue
        s.mark_cut_rendered(page, job, 0, asset_sha256="d"*64,
            rendered_at=next_day.isoformat(), path=path)
        break
    else:
        pytest.fail("could not create a legal separating actual nonreverse")
    return next_day, "already-queued-day-two"


def test_delayed_reverse_never_commits_twice_on_one_render_day(tmp_path):
    path, page = tmp_path / "history.json", "acct:reverse-delayed"
    next_day, job = queued_day_two_reverse_before_delayed_day_one_commit(path, page)
    with pytest.raises(s.ScramblerExhausted, match="reverse_render_day_exhausted"):
        s.mark_cut_rendered(page, job, 0, asset_sha256="e"*64,
            rendered_at=next_day.isoformat(), path=path)
    rendered = [row for row in s.scrambler_history_rows(page, path=path)
        if row["state"] == s.RENDERED and row["treatment"] == "reverse"
        and s.et_day(row["renderedAt"]) == s.et_day(next_day.isoformat())]
    assert len(rendered) == 1


def test_reverse_preflight_refuses_day_already_used_by_delayed_render(tmp_path):
    path, page = tmp_path / "history.json", "acct:reverse-preflight"
    next_day, job = queued_day_two_reverse_before_delayed_day_one_commit(path, page)
    before = path.read_bytes()
    with pytest.raises(s.ScramblerExhausted, match="reverse_render_day_exhausted"):
        s.assert_cut_renderable(page, job, 0, rendered_at=next_day.isoformat(), path=path)
    assert path.read_bytes() == before
    s.assert_cut_renderable(page, job, 0,
        rendered_at=(next_day+timedelta(days=1)).isoformat(), path=path)
    with pytest.raises(s.ScramblerExhausted, match="reverse_render_day_exhausted"):
        s.mark_cut_rendered(page, job, 0, asset_sha256="e"*64,
            rendered_at=next_day.isoformat(), path=path)
    assert path.read_bytes() == before


def test_draw_refuses_reverse_on_actual_day_of_prior_day_delayed_render(tmp_path):
    path, page = tmp_path / "history.json", "acct:reverse-draw-day"
    for i in range(14):
        assert commit(path, page, f"support-{i}", DAY).treatment != "reverse"
    assert s.reserve_job_cuts(page, "late", 1, now=DAY.isoformat(), path=path)[0].treatment == "reverse"
    next_day = DAY + timedelta(days=1)
    s.mark_cut_rendered(page, "late", 0, asset_sha256="b"*64,
        rendered_at=next_day.isoformat(), path=path)
    # Enough new rendered support would otherwise allow another reverse.
    # It must be refused at DRAW time, without reaching media/preflight.
    for i in range(35):
        job = f"next-day-{i}"
        draw = s.reserve_job_cuts(page, job, 1, now=next_day.isoformat(), path=path)[0]
        assert draw.treatment != "reverse", "actual render day already consumed"
        s.mark_cut_rendered(page, job, 0, asset_sha256="c"*64,
            rendered_at=next_day.isoformat(), path=path)
