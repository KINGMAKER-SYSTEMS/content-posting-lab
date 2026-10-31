"""A feed refresh must not unlock used source windows mid-publication."""
import json
import os
import threading
from pathlib import Path

import pytest

from services import scrambler_feed_pull as pull
from services import scrambler_originality as originality
from tests.test_scrambler_feed_pull import _feed_generation, _make_fetch, _manifest


@pytest.mark.parametrize("legacy", [False, True])
def test_refresh_never_exposes_new_master_without_its_posted_usage(tmp_path, monkeypatch, legacy):
    old = _feed_generation(['a'])
    new = _feed_generation(['a', 'b'])
    origins = json.loads(new['origins.json'])
    origins['c' * 64] = {'page': 'pagec', 'origin_kind': 'yt', 'origin_id': 'vidc00',
                         'origin_start_s': 0.0, 'origin_end_s': 9.0,
                         'evidence': 'manifest', 'confidence': 'high'}
    new['origins.json'] = json.dumps(origins).encode()
    new['manifest.json'] = _manifest(new['usage.jsonl'], new['origins.json'])
    dest = tmp_path / 'feed'
    dest.mkdir()
    for name, data in old.items():
        (dest / name).write_bytes(data)
    if legacy:
        (dest / 'manifest.json').unlink()
    retired = tmp_path / 'retired.json'
    retired.write_text('[]')
    monkeypatch.setenv(originality.ORIGINALITY_USAGE_PATH_ENV, str(dest / 'usage.jsonl'))
    monkeypatch.setenv(originality.ORIGINALITY_ORIGINS_PATH_ENV, str(dest / 'origins.json'))
    monkeypatch.setenv(originality.ORIGINALITY_RETIRED_PATH_ENV, str(retired))
    originality._index_cache.clear()
    def check(sha):
        return originality.load_originality_index_cached().window_allowed(
            'acct:pilot', sha * 64, 0, 3000, page_handles=frozenset({'@pilot'}))
    assert not check('b')[0]  # generation A cannot map B
    reached, release = threading.Event(), threading.Event()
    replace = os.replace
    def pause(src, dst):
        if str(src).endswith('.tmp') and Path(dst).name == 'usage.jsonl':
            reached.set()
            assert release.wait(5)
        return replace(src, dst)
    monkeypatch.setattr(pull.os, 'replace', pause)
    errors = []
    def writer():
        try:
            pull.pull_once(_make_fetch(new), dest)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert reached.wait(5)
        try:
            allowed, reason = check('b')  # actual cached reader used at pre-encode recheck
        except originality.ScramblerOriginalityUnavailable as error:
            allowed, reason = False, str(error)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert not check('b')[0]  # generation B carries B's recent other-page use
    assert check('c')[0]      # an unused, fully mapped master remains eligible
    assert not allowed, f'newly mapped already-posted B was allowed: {reason!r}'


def test_managed_missing_manifest_refuses_even_with_warm_cache(tmp_path, monkeypatch):
    feed = _feed_generation(['a'])
    dest = tmp_path / 'feed'
    pull.pull_once(_make_fetch(feed), dest)
    retired = tmp_path / 'retired.json'
    retired.write_text('[]')
    monkeypatch.setenv(originality.ORIGINALITY_USAGE_PATH_ENV, str(dest / 'usage.jsonl'))
    monkeypatch.setenv(originality.ORIGINALITY_RETIRED_PATH_ENV, str(retired))
    originality._index_cache.clear()
    originality.load_originality_index_cached()
    (dest / 'manifest.json').unlink()
    import pytest
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='manifest missing'):
        originality.load_originality_index_cached()


def test_warm_cache_rechecks_same_size_same_mtime_data(tmp_path, monkeypatch):
    feed = _feed_generation(['a'])
    dest = tmp_path / 'feed'
    pull.pull_once(_make_fetch(feed), dest)
    retired = tmp_path / 'retired.json'
    retired.write_text('[]')
    monkeypatch.setenv(originality.ORIGINALITY_USAGE_PATH_ENV, str(dest / 'usage.jsonl'))
    monkeypatch.setenv(originality.ORIGINALITY_RETIRED_PATH_ENV, str(retired))
    originality._index_cache.clear()
    originality.load_originality_index_cached()
    usage = dest / 'usage.jsonl'
    before = usage.stat()
    raw = usage.read_bytes()
    usage.write_bytes(raw.replace(b'@page', b'@xxxx', 1))
    assert usage.stat().st_size == before.st_size
    os.utime(usage, ns=(before.st_atime_ns, before.st_mtime_ns))
    import pytest
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='sha256_mismatch'):
        originality.load_originality_index_cached()


def test_first_publication_barrier_refuses_until_data_matches(tmp_path, monkeypatch):
    dest = tmp_path / 'feed'
    retired = tmp_path / 'retired.json'
    retired.write_text('[]')
    feed = _feed_generation(['a'])
    origins = json.loads(feed['origins.json'])
    origins['b' * 64] = {'page': 'pageb', 'origin_kind': 'yt', 'origin_id': 'vidb00',
                         'origin_start_s': 0.0, 'origin_end_s': 9.0}
    feed['origins.json'] = json.dumps(origins).encode()
    feed['manifest.json'] = _manifest(feed['usage.jsonl'], feed['origins.json'])
    reached, release = threading.Event(), threading.Event()
    replace = os.replace
    errors = []
    def pause(src, dst):
        result = replace(src, dst)
        if str(src).endswith('.tmp') and Path(dst).name == 'manifest.json':
            reached.set()
            assert release.wait(5)
        return result
    def writer():
        try:
            pull.pull_once(_make_fetch(feed), dest)
        except BaseException as error:
            errors.append(error)
    monkeypatch.setattr(pull.os, 'replace', pause)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert reached.wait(5)
        with pytest.raises(originality.ScramblerOriginalityUnavailable, match='usage_path_missing'):
            originality.load_originality_index(dest / 'usage.jsonl', retired_path=retired)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    index = originality.load_originality_index(dest / 'usage.jsonl', retired_path=retired)
    assert index.window_allowed('acct:pilot', 'b' * 64, 0, 3000, page_handles=frozenset({'@pilot'}))[0]


def test_verified_old_bytes_cannot_borrow_new_generation_mtime(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import pytest
    dest = tmp_path / 'feed'
    old = _feed_generation(['a'])
    old_time = datetime.now(timezone.utc) - timedelta(hours=27)
    old['manifest.json'] = _manifest(old['usage.jsonl'], old['origins.json'], generated_at=old_time.isoformat())
    pull.pull_once(_make_fetch(old), dest)
    retired = tmp_path / 'retired.json'
    retired.write_text('[]')
    monkeypatch.setenv(originality.ORIGINALITY_USAGE_PATH_ENV, str(dest / 'usage.jsonl'))
    monkeypatch.setenv(originality.ORIGINALITY_RETIRED_PATH_ENV, str(retired))
    originality._index_cache.clear()
    snapshot = originality._verified_feed_snapshot
    def swap_after_snapshot(*args):
        captured = snapshot(*args)
        pull.pull_once(_make_fetch(_feed_generation(['a', 'b'])), dest)
        return captured
    monkeypatch.setattr(originality, '_verified_feed_snapshot', swap_after_snapshot)
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='stale'):
        originality.load_originality_index_cached()
    monkeypatch.setattr(originality, '_verified_feed_snapshot', snapshot)
    assert 'b' * 64 in originality.load_originality_index_cached().origins


def test_broken_marker_cannot_masquerade_as_legacy(tmp_path):
    import pytest
    feed = _feed_generation(['a'])
    dest = tmp_path / 'feed'
    dest.mkdir()
    for name in ('usage.jsonl', 'origins.json'):
        (dest / name).write_bytes(feed[name])
    (dest / '.scrambler-feed.lock').symlink_to(dest / 'absent')
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='not a regular file'):
        originality._verified_feed_snapshot(dest / 'usage.jsonl', dest / 'origins.json')


def test_legacy_stable_two_file_feed_still_loads(tmp_path):
    feed = _feed_generation(['a'])
    for name in ('usage.jsonl', 'origins.json'):
        (tmp_path / name).write_bytes(feed[name])
    retired = tmp_path / 'retired.json'
    retired.write_text('[]')
    index = originality.load_originality_index(tmp_path / 'usage.jsonl', retired_path=retired)
    assert 'a' * 64 in index.origins


def test_marker_stat_error_refuses_not_legacy(tmp_path, monkeypatch):
    import pytest
    feed = _feed_generation(['a'])
    for name in ('usage.jsonl', 'origins.json'):
        (tmp_path / name).write_bytes(feed[name])
    real = os.lstat
    def denied(path):
        if Path(path).name == '.scrambler-feed.lock':
            raise PermissionError('injected')
        return real(path)
    monkeypatch.setattr(os, 'lstat', denied)
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='cannot inspect'):
        originality._verified_feed_snapshot(tmp_path / 'usage.jsonl', tmp_path / 'origins.json')


def test_bad_manifest_never_unlocks_managed_feed(tmp_path):
    import pytest
    pull.pull_once(_make_fetch(_feed_generation(['a'])), tmp_path)
    (tmp_path / 'manifest.json').write_text('{"generated_at":"invalid","rows":0,"sha256s":{}}')
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='feed_snapshot_invalid'):
        originality._verified_feed_snapshot(tmp_path / 'usage.jsonl', tmp_path / 'origins.json')


def test_writer_barrier_failure_leaves_data_untouched(tmp_path, monkeypatch):
    import pytest
    old, new = _feed_generation(['a']), _feed_generation(['a', 'b'])
    pull.pull_once(_make_fetch(old), tmp_path)
    actual = pull._fsync_dir_strict
    calls = 0
    def fail_manifest_barrier(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('injected barrier failure')
        return actual(path)
    monkeypatch.setattr(pull, '_fsync_dir_strict', fail_manifest_barrier)
    with pytest.raises(pull.ScramblerFeedPullError, match='write_failed'):
        pull.pull_once(_make_fetch(new), tmp_path)
    assert (tmp_path / 'usage.jsonl').read_bytes() == old['usage.jsonl']
    assert (tmp_path / 'origins.json').read_bytes() == old['origins.json']


def test_pull_and_manual_sync_serialize_on_same_marker(tmp_path, monkeypatch):
    from tools import scrambler_feed_sync
    import time
    dest, source = tmp_path / 'dest', tmp_path / 'source'
    source.mkdir()
    source_gen = _feed_generation(['a', 'b', 'c'])
    for name in ('usage.jsonl', 'origins.json'):
        (source / name).write_bytes(source_gen[name])
    reached, release, manual_done = threading.Event(), threading.Event(), threading.Event()
    real = os.replace
    def pause(src, dst):
        if Path(dst).name == 'origins.json' and not reached.is_set():
            reached.set()
            assert release.wait(5)
        return real(src, dst)
    monkeypatch.setattr(pull.os, 'replace', pause)
    errors = []
    def first():
        try:
            pull.pull_once(_make_fetch(_feed_generation(['a', 'b'])), dest)
        except BaseException as error:
            errors.append(error)
    def second():
        try:
            scrambler_feed_sync.sync_feed(source, dest)
        except BaseException as error:
            errors.append(error)
        finally:
            manual_done.set()
    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    try:
        assert reached.wait(5)
        two.start()
        assert not manual_done.wait(0.1)
    finally:
        release.set()
        one.join(5)
        if two.ident is not None:
            two.join(5)
    assert not one.is_alive() and not two.is_alive() and not errors
    usage, origins, _ = originality._verified_feed_snapshot(dest / 'usage.jsonl', dest / 'origins.json')
    assert usage == source_gen['usage.jsonl'] and origins == source_gen['origins.json']


def test_failed_rollback_keeps_manifest_and_reader_refuses(tmp_path, monkeypatch):
    import pytest
    old, new = _feed_generation(['a']), _feed_generation(['a', 'b'])
    pull.pull_once(_make_fetch(old), tmp_path)
    real = os.replace
    def fail(src, dst):
        if str(src).endswith('.tmp') and Path(dst).name == 'usage.jsonl':
            raise OSError('write failure')
        if str(src).endswith('.bak') and Path(dst).name == 'origins.json':
            raise OSError('rollback failure')
        return real(src, dst)
    monkeypatch.setattr(pull.os, 'replace', fail)
    with pytest.raises(pull.ScramblerFeedPullError):
        pull.pull_once(_make_fetch(new), tmp_path)
    assert (tmp_path / '.scrambler-feed.lock').exists()
    assert (tmp_path / 'manifest.json').read_bytes() == new['manifest.json']
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='sha256_mismatch'):
        originality._verified_feed_snapshot(tmp_path / 'usage.jsonl', tmp_path / 'origins.json')
    monkeypatch.setattr(pull.os, 'replace', real)
    pull.pull_once(_make_fetch(new), tmp_path)
    assert originality._verified_feed_snapshot(tmp_path / 'usage.jsonl', tmp_path / 'origins.json')[0] == new['usage.jsonl']


def test_manual_sync_preserves_source_age_and_refuses_stale(tmp_path):
    import time
    import pytest
    from tools import scrambler_feed_sync
    source, dest = tmp_path / 'source', tmp_path / 'dest'
    source.mkdir()
    old = _feed_generation(['a'])
    for name in ('usage.jsonl', 'origins.json'):
        (source / name).write_bytes(old[name])
    age = time.time() - 3600
    os.utime(source / 'usage.jsonl', (age, age))
    scrambler_feed_sync.sync_feed(source, dest)
    assert abs((dest / 'usage.jsonl').stat().st_mtime - age) < 0.01
    os.utime(source / 'usage.jsonl', (age - 30 * 3600, age - 30 * 3600))
    with pytest.raises(scrambler_feed_sync.FeedSyncRefused, match='stale'):
        scrambler_feed_sync.sync_feed(source, dest)


def test_manual_sync_reports_verified_generation_age(tmp_path):
    import time
    from datetime import datetime, timezone
    from tools import scrambler_feed_sync
    now = int(time.time())
    source, dest = tmp_path / 'source', tmp_path / 'dest'
    feed = _feed_generation(['a'])
    feed['manifest.json'] = _manifest(feed['usage.jsonl'], feed['origins.json'],
        generated_at=datetime.fromtimestamp(now - 3600, timezone.utc).isoformat())
    pull.pull_once(_make_fetch(feed), source, now_epoch=now)
    os.utime(source / 'usage.jsonl', (now, now))
    result = scrambler_feed_sync.sync_feed(source, dest, now_epoch=now)
    assert result['usageAgeSeconds'] == 3600
    assert abs((dest / 'usage.jsonl').stat().st_mtime - (now - 3600)) < 0.01


def test_unchanged_cache_reuses_parse_but_enforces_age(tmp_path, monkeypatch):
    import time
    feed = _feed_generation(['a'])
    pull.pull_once(_make_fetch(feed), tmp_path)
    retired = tmp_path / 'retired.json'
    retired.write_text('[]')
    monkeypatch.setenv(originality.ORIGINALITY_USAGE_PATH_ENV, str(tmp_path / 'usage.jsonl'))
    monkeypatch.setenv(originality.ORIGINALITY_RETIRED_PATH_ENV, str(retired))
    originality._index_cache.clear()
    now = time.time()
    first = originality.load_originality_index_cached(now_epoch=now)
    original_load = originality.load_originality_index
    def no_reparse(*args, **kwargs):
        raise AssertionError('unchanged cache should reuse parsed index')
    monkeypatch.setattr(originality, 'load_originality_index', no_reparse)
    assert originality.load_originality_index_cached(now_epoch=now + 1) is first
    monkeypatch.setattr(originality, 'load_originality_index', original_load)
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match='stale'):
        originality.load_originality_index_cached(now_epoch=now + 27 * 3600)


def test_retired_master_remains_refused_in_verified_generation(tmp_path, monkeypatch):
    feed = _feed_generation(['a'])
    pull.pull_once(_make_fetch(feed), tmp_path)
    retired = tmp_path / 'retired.json'
    retired.write_text(json.dumps([{'master_sha256s': ['a' * 64]}]))
    monkeypatch.setenv(originality.ORIGINALITY_USAGE_PATH_ENV, str(tmp_path / 'usage.jsonl'))
    monkeypatch.setenv(originality.ORIGINALITY_RETIRED_PATH_ENV, str(retired))
    originality._index_cache.clear()
    allowed, reason = originality.load_originality_index_cached().window_allowed(
        'acct:pilot', 'a' * 64, 0, 3000, page_handles=frozenset({'@pilot'}))
    assert not allowed and reason.startswith('retired_master:')
