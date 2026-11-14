"""Class guard: the pool-fed (Worker-supplied) page id set is exact.

The pool-supplied list in ``recipes/pool-supplied-pages.v1.json`` is the Lab's
closed source of truth for pages whose finished clips come from the Worker's
vault pool rather than Lab-rendered media. A dotted handle like ``tis.the.szn7``
slugs to the page id ``acct:tis-the-szn7`` (``services/notion_pages.py``
``mint_integration_id``: every non [a-z0-9] run becomes ``-``). Because the
list is matched EXACTLY (``services/pool_supplied_pages.is_pool_supplied``),
a dotted or underscore form must never silently count as supplied, and any
added, removed, or misspelled id must fail CI.

WHO CHANGES IT / HOW: fourteen-page owns the pool-fed roster. To change the
roster, edit BOTH ``recipes/pool-supplied-pages.v1.json`` and the literal
``POOL_FED_PAGE_IDS`` set below in the same commit. This test turns red until
the two agree exactly.
"""

from __future__ import annotations

import pytest

import services.pool_supplied_pages as psp
from services.pool_supplied_pages import (
    is_pool_supplied,
    load_pool_supplied_page_ids,
)

# The exact committed pool-fed set: 12 original pages + 3 replacement Warner
# pages + Goldford sunsetsoul46 + Christmas tis-the-szn7 (11-03). Keep this
# literal set in lockstep with recipes/pool-supplied-pages.v1.json.
POOL_FED_PAGE_IDS = frozenset({
    "acct:chemically-lit",
    "acct:living-our-lives6",
    "acct:fly-baby76",
    "acct:cody-szn6",
    "acct:all-i-want-for-xma",
    "acct:joy-to-the-world-003",
    "acct:the-lyrics-tho7",
    "acct:text-on-block",
    "acct:onrepeat-radio",
    "acct:glossytok4",
    "acct:grey-szn03",
    "acct:wildflower993",
    "acct:the-lyrics-tho5",
    "acct:wildflower1234321",
    "acct:joy-to-the-world709",
    "acct:sunsetsoul46",
    "acct:tis-the-szn7",
})


@pytest.fixture(autouse=True)
def _fresh_pool_cache():
    # Other tests monkeypatch psp._PATH; the loader caches per path, but reset
    # anyway so this guard always reads the committed list.
    psp.reset_pool_supplied_page_ids_cache()
    yield
    psp.reset_pool_supplied_page_ids_cache()


def test_pool_fed_page_id_set_matches_committed_list_exactly():
    # Exact equality (not subset, not length) so an added, removed, or
    # misspelled id fails rather than slipping through.
    assert load_pool_supplied_page_ids() == POOL_FED_PAGE_IDS


def test_dotted_handle_forms_never_match_by_normalisation():
    # The TikTok handle tis.the.szn7 slugs to the page id acct:tis-the-szn7.
    # Matching is exact: a dotted or underscore form is a different id, not a
    # normalised equivalent, so it must NOT be treated as pool-supplied.
    assert is_pool_supplied("acct:tis-the-szn7") is True
    assert is_pool_supplied("acct:tis.the.szn7") is False
    assert is_pool_supplied("acct:tis_the_szn7") is False


def test_pool_fed_page_ids_match_exactly_not_by_prefix_or_suffix():
    for page_id in (
        "acct:tis-the-szn7x",
        "acct:tis-the-szn",
        "acct:chemically-litx",
        "acct:chemically-lit.",
        "tis-the-szn7",
        "acct:tis-the-szn7:extra",
    ):
        assert is_pool_supplied(page_id) is False, page_id
