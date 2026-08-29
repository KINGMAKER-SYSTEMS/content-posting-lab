"""Shared lossless Master Pages fixtures for control-plane contract tests."""

from __future__ import annotations

from services.master_pages_contract import intent_hash


def master_pages(
    page_id: str,
    *,
    handle: str = "test.page",
    content_niche: str = "TRUCK",
    content_engine: str = "ai_video",
    vault_url: str = "https://drive.example/test-page",
) -> tuple[dict, str]:
    intent = {
        "schema": "master-pages.page-intent.v1",
        "pageId": page_id,
        "handle": handle,
        "group": "INTERNAL",
        "groupLabel": "Internal",
        "pageType": "TikTok",
        "accountType": "TRUCK",
        "posterName": "PIXEL-1",
        "status": "Active",
        "project": None,
        "tiktokUrl": f"https://www.tiktok.com/@{handle}",
        "notionPageId": f"notion-{page_id}",
        "source": "notion",
        "contentNiche": content_niche,
        "contentEngine": content_engine,
        "automationMode": "Automation",
        "vaultUrl": vault_url,
        "pipeline": "Content Lab",
        "soundsReference": None,
        "archived": False,
    }
    return intent, intent_hash(intent)


def bind_current_intent(monkeypatch, module, intent: dict, revision: str) -> None:
    monkeypatch.setattr(
        module,
        "_current_master_pages_intent",
        lambda page_id: (intent, revision) if page_id == intent["pageId"] else None,
    )
