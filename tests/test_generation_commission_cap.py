"""Commission-scoped provider-create cap (Dale eight: 16 attempts / $4.48).

Every paid create on the control-plane path is reserved through
debit_generation_spend_at BEFORE its HTTP POST, with an idempotent id
``<labjob>-gNN[-rM]:s0 | :s0:httpN | :paN | :paN:httpN``. A job bound to a
configured commission must also fit that commission's lifetime attempt and USD
caps inside the same transaction, or the create is refused before it is sent.
Unset configuration keeps today's behaviour and never alters the ledger.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from services import generation_budget as gb

PAGE = "acct:dale-hargrove31"
CAPS = [{"id": "dale-e7d53659", "pageId": PAGE, "maxAttempts": 16, "maxUsd": "4.48"}]


def _set(monkeypatch, caps=CAPS, daily="175"):
    monkeypatch.setenv(gb.USD_BUDGET_ENV, daily)
    if caps is None:
        monkeypatch.delenv(gb.COMMISSION_CAPS_ENV, raising=False)
    else:
        monkeypatch.setenv(gb.COMMISSION_CAPS_ENV, json.dumps(caps))


def _tables(store):
    db = sqlite3.connect(gb.ledger_path(store))
    try:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def test_unset_config_binds_nothing_and_leaves_the_ledger_shape_unchanged(monkeypatch, tmp_path):
    _set(monkeypatch, caps=None)
    store = tmp_path / "jobs.json"
    assert gb.bind_commission_job(store, "cpl-aaaa", PAGE) is None
    for n in range(20):
        assert gb.debit_generation_spend_at(store, 0.28, f"cpl-aaaa-g{n:02d}:s0") is True
    assert "commission_jobs" not in _tables(store) and "commission_debits" not in _tables(store)


def test_bound_job_gets_exactly_sixteen_creates_then_refusal_before_any_spend(monkeypatch, tmp_path):
    _set(monkeypatch)
    store = tmp_path / "jobs.json"
    assert gb.bind_commission_job(store, "cpl-dale1", PAGE) == "dale-e7d53659"
    for n in range(16):
        assert gb.debit_generation_spend_at(store, 0.28, f"cpl-dale1-g{n:02d}:s0") is True
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale1-g16:s0") is False
    assert gb.spent_usd_at(store) == pytest.approx(16 * 0.28)
    assert gb.commission_summary_at(store, "dale-e7d53659") == {
        "id": "dale-e7d53659", "attempts": 16, "usd": "4.48", "maxAttempts": 16, "maxUsd": "4.48"}


def test_every_retry_shape_counts_moderation_http_and_pa(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 6}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-dale2", PAGE)
    ids = ["cpl-dale2-g00:s0", "cpl-dale2-g00:s0:http1", "cpl-dale2-g00:pa1",
           "cpl-dale2-g00:pa1:http1", "cpl-dale2-g00-r1:s0", "cpl-dale2-g00-r1:s0:http3"]
    assert all(gb.debit_generation_spend_at(store, 0.28, debit) for debit in ids)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale2-g00-r2:s0") is False


def test_usd_cap_binds_independently_of_the_attempt_cap(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 99, "maxUsd": "0.84"}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-dale3", PAGE)
    for n in range(3):
        assert gb.debit_generation_spend_at(store, 0.28, f"cpl-dale3-g00:s0:http{n}") is True
    assert gb.debit_generation_spend_at(store, 0.01, "cpl-dale3-g01:s0") is False


def test_known_id_resume_is_free_even_at_the_cap(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 1}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-dale4", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale4-g00:s0") is True
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale4-g00:s0") is True
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale4-g00:s0:http1") is False
    assert gb.commission_summary_at(store, "dale-e7d53659")["attempts"] == 1


def test_two_jobs_of_one_commission_share_one_cap(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 3}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-sun", PAGE)
    gb.bind_commission_job(store, "cpl-sat", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-sun-g00:s0")
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-sat-g00:s0")
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-sun-g01:s0")
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-sat-g01:s0") is False


def test_utc_rollover_never_resets_the_commission(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 2}])
    store = tmp_path / "jobs.json"
    day1 = datetime(2026, 11, 14, 23, 59, tzinfo=timezone.utc)
    day2 = datetime(2026, 11, 15, 0, 1, tzinfo=timezone.utc)
    gb.bind_commission_job(store, "cpl-dale5", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale5-g00:s0", now=day1)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale5-g01:s0", now=day2)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale5-g02:s0", now=day2) is False


def test_other_pages_and_unbound_jobs_are_unaffected(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 1}])
    store = tmp_path / "jobs.json"
    assert gb.bind_commission_job(store, "cpl-rusty", "acct:rusty") is None
    gb.bind_commission_job(store, "cpl-dale6", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale6-g00:s0")
    for n in range(5):
        assert gb.debit_generation_spend_at(store, 0.28, f"cpl-rusty-g{n:02d}:s0") is True
        assert gb.debit_generation_spend_at(store, 0.28, f"recreate:{n}") is True


def test_daily_cap_still_applies_and_a_daily_refusal_counts_nothing(monkeypatch, tmp_path):
    _set(monkeypatch, daily="0.30")
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-dale7", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale7-g00:s0") is True
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale7-g01:s0") is False
    assert gb.commission_summary_at(store, "dale-e7d53659")["attempts"] == 1


def test_a_commission_refusal_writes_no_debit_and_no_day_spend(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 1}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-dale8", PAGE)
    gb.debit_generation_spend_at(store, 0.28, "cpl-dale8-g00:s0")
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale8-g01:s0") is False
    db = sqlite3.connect(gb.ledger_path(store))
    try:
        assert [r[0] for r in db.execute("SELECT id FROM debits ORDER BY id")] == ["cpl-dale8-g00:s0"]
    finally:
        db.close()


def test_bound_job_whose_commission_is_no_longer_configured_fails_closed(monkeypatch, tmp_path):
    _set(monkeypatch)
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-dale9", PAGE)
    _set(monkeypatch, caps=[])
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale9-g00:s0") is False


def test_binding_is_immutable_and_rebinding_is_idempotent(monkeypatch, tmp_path):
    _set(monkeypatch)
    store = tmp_path / "jobs.json"
    assert gb.bind_commission_job(store, "cpl-dale10", PAGE) == "dale-e7d53659"
    assert gb.bind_commission_job(store, "cpl-dale10", PAGE) == "dale-e7d53659"
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-dale10-g00:s0") is True
    db = sqlite3.connect(gb.ledger_path(store))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM commission_jobs")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE commission_debits SET amount_usd='0'")
    finally:
        db.close()


@pytest.mark.parametrize("raw", [
    "not json", "{}", "[1]", '[{"id":"x","pageId":"p","maxAttempts":0,"maxUsd":"1"}]',
    '[{"id":"x","pageId":"p","maxAttempts":1,"maxUsd":"-1"}]',
    '[{"id":"x","pageId":"p","maxAttempts":1,"maxUsd":"nan"}]',
    '[{"id":"x","pageId":"p","maxAttempts":true,"maxUsd":"1"}]',
    '[{"id":"x","pageId":"p","maxAttempts":1,"maxUsd":"1"},{"id":"y","pageId":"p","maxAttempts":1,"maxUsd":"1"}]',
])
def test_malformed_configuration_fails_closed(monkeypatch, tmp_path, raw):
    monkeypatch.setenv(gb.COMMISSION_CAPS_ENV, raw)
    with pytest.raises(ValueError):
        gb.bind_commission_job(tmp_path / "jobs.json", "cpl-x", "p")


# ---- Provider-adapter retries (Codex review of 99ff8f52, REPORT 8ce9621a) ----
# generate_one scopes the provider job id as "<labjob>-gNN#<index>"
# (providers/base.py), so HTTP create retries debit "...#0:s0:httpN" and a PA
# resubmit debits "...#0:paN". Those creates must count against the commission.

import httpx  # noqa: E402

from providers import base as provider_base  # noqa: E402
from providers import replicate  # noqa: E402


def _exhausted_commission(monkeypatch, tmp_path, *, bind=True):
    store = tmp_path / "jobs.json"
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 1, "maxUsd": "100"}])
    monkeypatch.setattr(gb, "jobs_store_path", lambda: store)
    monkeypatch.setitem(replicate.API_KEYS, "replicate", "dummy")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(replicate, "_sleep", no_sleep)
    if bind:
        assert gb.bind_commission_job(store, "cpl-gen", PAGE) == "dale-e7d53659"
    # The executor reserves the first create before generate_one is called.
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-gen-g00:s0") is True
    return store


def _run_generate_one(monkeypatch, tmp_path, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(provider_base.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    jobs = {"cpl-gen-g00": {"videos": [{"index": 0, "status": "queued"}]}}
    import asyncio
    asyncio.run(provider_base.generate_one(
        "cpl-gen-g00", 0, "hailuo", "a dark truck by a pond", "9:16", "768p", 6, None,
        jobs, tmp_path / "out", "", cost_usd=0.28))
    return jobs["cpl-gen-g00"]["videos"][0]


def test_generate_one_scopes_the_provider_job_id_with_its_index():
    import inspect
    assert '"job_id": f"{job_id}#{index}"' in inspect.getsource(provider_base.generate_one)


def test_http_create_retry_through_generate_one_never_passes_an_exhausted_commission(monkeypatch, tmp_path):
    store = _exhausted_commission(monkeypatch, tmp_path)
    posts = []

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/predictions"):
            posts.append(request)
            return httpx.Response(503 if len(posts) == 1 else 201, json={"id": "p1"})
        return httpx.Response(200, json={"status": "succeeded", "output": "https://example.invalid/v.mp4"})

    entry = _run_generate_one(monkeypatch, tmp_path, handler)
    assert len(posts) == 1, "the 503 retry was refused before a second POST"
    assert entry["status"] == "error"
    assert gb.commission_summary_at(store, "dale-e7d53659")["attempts"] == 1


def test_pa_resubmit_through_generate_one_never_passes_an_exhausted_commission(monkeypatch, tmp_path):
    store = _exhausted_commission(monkeypatch, tmp_path)
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"id": "p1"})
        return httpx.Response(200, json={"status": "failed", "error": "Prediction interrupted (code: PA)"})

    entry = _run_generate_one(monkeypatch, tmp_path, handler)
    assert len(posts) == 1, "the PA resubmit was refused before a second POST"
    assert entry["status"] == "error"
    assert gb.commission_summary_at(store, "dale-e7d53659")["attempts"] == 1


def test_control_unbound_job_still_gets_its_pa_resubmit(monkeypatch, tmp_path):
    _exhausted_commission(monkeypatch, tmp_path, bind=False)
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"id": f"p{len(posts)}"})
        return httpx.Response(200, json={"status": "failed", "error": "Prediction interrupted (code: PA)"})

    _run_generate_one(monkeypatch, tmp_path, handler)
    assert len(posts) == 2, "an unbound job keeps today's single PA resubmit"


@pytest.mark.parametrize("debit", [
    "cpl-gen-g00#0:s0:http1", "cpl-gen-g00#0:pa1", "cpl-gen-g00#0:pa1:http3",
    "cpl-gen-g00-r2#0:s0:http2", "cpl-gen-g07#3:pa1",
])
def test_every_adapter_debit_shape_maps_to_the_bound_job(monkeypatch, tmp_path, debit):
    store = _exhausted_commission(monkeypatch, tmp_path)
    assert gb.debit_generation_spend_at(store, 0.28, debit) is False


def test_a_removed_commission_also_fails_closed_for_adapter_retries(monkeypatch, tmp_path):
    store = _exhausted_commission(monkeypatch, tmp_path)
    _set(monkeypatch, caps=[])
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-gen-g00#0:s0:http1") is False


def test_two_connections_race_at_the_cap_edge(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 2}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-race", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, "cpl-race-g00:s0")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: gb.debit_generation_spend_at(store, 0.28, f"cpl-race-g0{n}#0:s0:http1"), (1, 2)))
    assert sorted(results) == [False, True]
    assert gb.commission_summary_at(store, "dale-e7d53659")["attempts"] == 2


# ---- Exact job ownership (Codex review of 9ae5d947, REPORT 3723844e) ----

def test_unbound_job_whose_id_extends_a_bound_id_is_not_charged_to_it(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{"id": "short", "pageId": "dale", "maxAttempts": 1, "maxUsd": "100"}])
    store = tmp_path / "jobs.json"
    assert gb.bind_commission_job(store, "abc", "dale") == "short"
    assert gb.debit_generation_spend_at(store, 0.28, "abc-g00:s0")
    assert gb.debit_generation_spend_at(store, 0.28, "abc-g01x-g00:s0") is True
    assert gb.debit_generation_spend_at(store, 0.28, "abc-g01x-g00#0:pa1:http2") is True
    assert gb.commission_summary_at(store, "short")["attempts"] == 1


def test_overlapping_bound_jobs_keep_their_own_commission(monkeypatch, tmp_path):
    _set(monkeypatch, caps=[{"id": "short", "pageId": "dale", "maxAttempts": 1, "maxUsd": "100"},
                            {"id": "long", "pageId": "other", "maxAttempts": 2, "maxUsd": "100"}])
    store = tmp_path / "jobs.json"
    assert gb.bind_commission_job(store, "abc", "dale") == "short"
    assert gb.bind_commission_job(store, "abc-g01x", "other") == "long"
    assert gb.debit_generation_spend_at(store, 0.28, "abc-g00:s0")
    assert gb.debit_generation_spend_at(store, 0.28, "abc-g01x-g00:s0")
    assert gb.debit_generation_spend_at(store, 0.28, "abc-g01x-g00-r1#0:s0:http1")
    assert gb.commission_summary_at(store, "short")["attempts"] == 1
    assert gb.commission_summary_at(store, "long")["attempts"] == 2


@pytest.mark.parametrize("debit", ["cpl-gen-g00:s0:retry1", "cpl-gen-g00#x:pa1", "cpl-gen-g0:s0", "cpl-gen-g00"])
def test_an_unknown_shape_beginning_with_a_bound_job_fails_closed(monkeypatch, tmp_path, debit):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 99}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-gen", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, debit) is False


@pytest.mark.parametrize("debit", ["cpl-other-g00:s0:retry1", "recreate:abc", "ui-job#0:pa1"])
def test_unknown_shapes_of_unbound_jobs_are_unaffected(monkeypatch, tmp_path, debit):
    _set(monkeypatch, caps=[{**CAPS[0], "maxAttempts": 1}])
    store = tmp_path / "jobs.json"
    gb.bind_commission_job(store, "cpl-gen", PAGE)
    assert gb.debit_generation_spend_at(store, 0.28, debit) is True
