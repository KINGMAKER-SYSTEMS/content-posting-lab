"""Independent real-producer history contracts; no application helpers redefined."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from services import scrambler_originality as originality

NOW = datetime.now(timezone.utc)
SHA_A, SHA_B = 'a' * 64, 'b' * 64


def feed(tmp_path, rows=(), *, origins=None, retired=()):
    usage = tmp_path / 'usage.jsonl'
    usage.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    mapping = origins or {sha: {'page': '@pilot', 'origin_kind': 'drive',
        'origin_id': 'CaseSensitiveID', 'origin_start_s': 600, 'origin_end_s': 1200}
        for sha in (SHA_A, SHA_B)}
    (tmp_path / 'origins.json').write_text(json.dumps(mapping))
    retired_path = tmp_path / 'retired.json'
    retired_path.write_text(json.dumps(list(retired)))
    return originality.load_originality_index(usage, retired_path=retired_path,
                                               now_epoch=NOW.timestamp())


def show(days=0, *, page='@other', start=600, end=603,
         origin_id='CaseSensitiveID', sha=SHA_A):
    return {'page': page, 'posted_at': (NOW - timedelta(days=days)).isoformat(),
            'master_sha': sha, 'origin_kind': 'drive', 'origin_id': origin_id,
            'origin_start_s': start, 'origin_end_s': end}


def allowed(index, start=600000, end=603000, sha=SHA_B):
    return index.window_allowed('acct:pilot', sha, start, end,
        page_handles=frozenset({'@pilot'}), now_epoch=NOW.timestamp())[0]


def test_unknown_prior_window_never_means_fresh_origin(tmp_path):
    try:
        index = feed(tmp_path, [show(start=None, end=None)])
    except originality.ScramblerOriginalityUnavailable:
        return  # refusing the relevant unknown feed is also safe
    assert not allowed(index), 'known other-page origin with lost window cannot unlock reuse'


@pytest.mark.parametrize(('start', 'end'), [(0, 3000), (599000, 602000),
    (1199000, 1201000), (603000, 600000), (600000, 600000), (-1000, 2000)])
def test_candidate_must_have_positive_window_inside_canonical_master_mapping(tmp_path, start, end):
    assert not allowed(feed(tmp_path), start, end)


def test_second_same_page_show_after_eight_days_is_legal(tmp_path):
    assert allowed(feed(tmp_path, [show(days=8, page='@PiLoT')]))


def test_third_same_page_show_within_thirty_days_is_refused(tmp_path):
    assert not allowed(feed(tmp_path, [show(days=8, page='@pilot'),
                                     show(days=16, page='@pilot')]))


def test_recent_show_through_another_master_sha_still_refuses(tmp_path):
    assert not allowed(feed(tmp_path, [show(days=1, sha=SHA_A)]), sha=SHA_B)


def test_case_sensitive_origin_identity_is_preserved(tmp_path):
    assert allowed(feed(tmp_path, [show(days=1, origin_id='casesensitiveid')]))


def test_retired_origin_alias_blocks_other_master_sha(tmp_path):
    index = feed(tmp_path, retired=[{'master_sha256s': [SHA_A]}])
    assert not allowed(index, sha=SHA_B)


def test_index_entry_budget_counts_duplicate_show_entries_before_expansion(tmp_path, monkeypatch):
    # Tiny fixture exercises aggregate cost, never allocate a giant ledger.
    monkeypatch.setattr(originality, 'USAGE_MAX_INDEX_ENTRIES', 5, raising=False)
    with pytest.raises(originality.ScramblerOriginalityUnavailable):
        feed(tmp_path, [show(days=1, start=600, end=603),
                        show(days=2, start=600, end=603)])
