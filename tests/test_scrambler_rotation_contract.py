"""Strong rotation/accounting pins independent of random collision luck."""
import os
import pytest
from services import scrambler as s


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("SCRAMBLER_"):
            monkeypatch.delenv(key, raising=False)


def test_changing_only_offset_never_unlocks_a_used_four_part_combo(tmp_path, monkeypatch):
    a = s.ScramblerDraw("none", 0.8, 8000, 0, False)
    b = s.ScramblerDraw("camera", 0.75, 7500, 0, False)
    shifted_a = s.ScramblerDraw("none", 0.8, 8000, 100, False)
    forced = [a]
    monkeypatch.setattr(s, "_candidate", lambda *args, **kwargs: forced[0])
    path = tmp_path / "history.json"
    def draw(job):
        return s.reserve_job_cuts("acct:combo", job, 1, path=path,
            grade_allowed=False, now="2026-10-29T12:00:00+00:00")[0]
    assert draw("a").combo() == a.combo()
    forced[0] = b
    assert draw("b").combo() == b.combo()
    forced[0] = shifted_a
    actual = draw("attempt-reuse")
    assert actual.combo() != a.combo()
    assert actual.combo() != b.combo()


@pytest.mark.parametrize(("speed", "length", "origin"), [
    (0.3, 5000, 1500), (0.3, 9000, 2700), (0.8, 5000, 4000),
    (0.8, 9000, 7200), (1.1, 5000, 5500), (1.1, 9000, 9900),
    (1.3, 5000, 6500), (1.3, 9000, 11700),
])
def test_origin_span_is_actual_output_times_effective_speed(speed, length, origin):
    draw = s.ScramblerDraw("none", speed, length, 900, False)
    assert draw.window_ms() == origin
    assert draw.span_ms() == origin + 900


def test_worker_source_window_limit_bounds_every_drawable_pair():
    """The Worker admits only origin windows of 5-10 s (sourceVideoBounds at
    Worker main 91f4e1a). A (speed, length) pair is drawable exactly when
    round(length x speed) is in 5000..10000 ms: slow 0.6-0.8x, fast 1.1-1.3x."""
    admitted = {(v, ms) for v in s.SPEEDS for ms in s.LENGTHS if s.worker_admits_window(v, ms)}
    for v in s.SPEEDS:
        for ms in s.LENGTHS:
            assert ((v, ms) in admitted) == (5000 <= int(round(ms * v)) <= 10000), (v, ms)
    assert min(v for v, _ in admitted) == 0.6
    assert (0.55, 9000) not in admitted and (0.6, 8000) not in admitted
    assert (0.6, 8500) in admitted and (1.3, 7500) in admitted and (1.3, 8000) not in admitted
    assert len(admitted) == 42                      # 20 slow + 22 fast pairs


def test_worker_source_window_limit_holds_for_drawn_cuts(tmp_path):
    path = tmp_path / "history.json"
    for i in range(40):
        draw = s.reserve_job_cuts("acct:window", f"job-{i}", 1, path=path,
            now="2026-10-29T12:00:00+00:00")[0]
        assert 5000 <= draw.window_ms() <= 10000, draw
        assert draw.speed >= 0.6, draw


def test_smallest_supported_branch_rotates_past_its_lifetime_vocabulary(tmp_path, monkeypatch):
    # All-fast + no grade has only 132 Worker-admissible nonreverse four-part
    # combos (22 speed/length pairs x 3 treatments x mirror). Run beyond
    # that with REAL rendered credits. The active rotation must age old combos
    # out, while length/treatment and active-window no-repeat remain strict.
    monkeypatch.setenv("SCRAMBLER_FAST_SHARE", "1")
    path = tmp_path / "history.json"
    horizon = s.ACTIVE_ROTATION_ROWS
    # Excluding previous length+treatment leaves at least 76 legal default
    # nonreverse choices in this branch. A horizon below 76 always leaves room.
    assert 0 < horizon < 76
    previous = []
    for i in range(200):
        draw = s.reserve_job_cuts("acct:long-term", f"job-{i}", 1, path=path,
            grade_allowed=False, now="2026-10-29T12:00:00+00:00")[0]
        assert draw.speed in (1.1, 1.2, 1.3)
        assert draw.combo() not in [item.combo() for item in previous[-horizon:]]
        if previous:
            assert draw.length_ms != previous[-1].length_ms
            assert draw.treatment != previous[-1].treatment
        s.mark_cut_rendered("acct:long-term", f"job-{i}", 0, path=path,
            asset_sha256="a" * 64, rendered_at="2026-10-29T12:00:00+00:00")
        previous.append(draw)
