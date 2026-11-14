"""The control-plane executor reserves every paid create of a commission job.

Real _run_dossier_generation with a mocked provider (dummy keys only): a page
with a configured commission is bound before its first create; a create that
would exceed the commission is refused before the provider is called. A page
without a commission keeps today's behaviour exactly.
"""

import json

import pytest

import routers.control_plane as cp
from services import generation_budget as gb
from tests.test_control_plane_dossier_execution import PAGE_ID, lab  # noqa: F401 (fixture)
from tests.test_generation_moderation_isolation import MODERATION, install_provider, queue_silhouettes


def _caps(monkeypatch, max_attempts, page=PAGE_ID):
    monkeypatch.setenv(gb.COMMISSION_CAPS_ENV, json.dumps(
        [{"id": "commission-test", "pageId": page, "maxAttempts": max_attempts, "maxUsd": "100"}]))


@pytest.mark.asyncio
async def test_moderation_retry_beyond_the_commission_is_never_sent(lab, monkeypatch):
    _caps(monkeypatch, 1)
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    calls = []
    install_provider(monkeypatch, [MODERATION, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    assert [call["id"] for call in calls] == [f"{job_id}-g00"], "the varied retry was refused before the provider"
    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "failed"
    summary = gb.commission_summary_at(gb.jobs_store_path(), "commission-test")
    assert (summary["attempts"], summary["maxAttempts"]) == (1, 1)
    before = len(calls)
    await cp._run_dossier_generation(job_id)
    assert len(calls) == before, "a rerun never spends past the commission"


@pytest.mark.asyncio
async def test_commission_with_room_lets_the_retry_through_and_counts_both(lab, monkeypatch):
    _caps(monkeypatch, 2)
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    calls = []
    install_provider(monkeypatch, [MODERATION, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g00-r1"]
    assert cp._load_jobs()["jobs"][job_id]["status"] == "completed"
    assert gb.commission_summary_at(gb.jobs_store_path(), "commission-test")["attempts"] == 2


@pytest.mark.asyncio
async def test_page_without_a_commission_is_unchanged(lab, monkeypatch):
    _caps(monkeypatch, 1, page="acct:some-other-page")
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    calls = []
    install_provider(monkeypatch, [MODERATION, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g00-r1"]
    assert gb.commission_summary_at(gb.jobs_store_path(), "commission-test")["attempts"] == 0


@pytest.mark.asyncio
async def test_malformed_commission_config_fails_the_job_before_any_create(lab, monkeypatch):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    monkeypatch.setenv(gb.COMMISSION_CAPS_ENV, "not json")
    calls = []
    install_provider(monkeypatch, ["done"], calls)
    await cp._run_dossier_generation(job_id)
    assert calls == []
    assert cp._load_jobs()["jobs"][job_id]["status"] == "failed"
