"""Private puller/reader seam using the repository's redacted captured shape."""
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import originality_shadow as reader
from services import scrambler_feed_pull as pull

FIXTURE = Path(__file__).parent / 'fixtures' / 'originality-usage-summary.v1.json'
STAMP = datetime(2027, 1, 15, 12, tzinfo=timezone.utc)
NOW = STAMP.timestamp() + 60


def generation(*, delta=0, change=None, manifest_change=None):
    document = json.loads(FIXTURE.read_bytes())
    document['generated_at'] = (STAMP + timedelta(seconds=delta)).isoformat()
    if change:
        document.update(change)
    data = json.dumps(document).encode()
    manifest = {'generated_at': document['generated_at'], 'rows': 0,
                'usage_summary_generated_at': document['generated_at'],
                'sha256s': {pull.SUMMARY_NAME: hashlib.sha256(data).hexdigest()}}
    if manifest_change:
        manifest.update(manifest_change)
    objects = {'manifest.json': json.dumps(manifest).encode(), pull.SUMMARY_NAME: data}
    return objects, data


def test_verified_pull_publishes_exact_bytes_consumed_by_current_reader(tmp_path):
    objects, data = generation()
    destination = tmp_path / 'summary.json'
    requested = []
    def fetch(name):
        requested.append(name)
        return objects[name]
    pull.pull_summary_once(fetch, destination, now_epoch=NOW)
    assert requested == ['manifest.json', pull.SUMMARY_NAME]
    assert destination.read_bytes() == data
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    parsed = reader._parse_summary(destination.read_bytes(), NOW)
    assert parsed.origins and parsed.masters
    assert parsed.generated_at_ts == STAMP.timestamp()


@pytest.mark.parametrize('fault', ['digest', 'binding', 'future', 'stale', 'shape', 'fetch'])
def test_failed_validation_keeps_previous_reader_bytes(tmp_path, fault):
    destination = tmp_path / 'summary.json'
    good, previous = generation()
    pull.pull_summary_once(good.__getitem__, destination, now_epoch=NOW)
    if fault == 'future':
        objects, _ = generation(delta=120)
    elif fault == 'stale':
        objects, _ = generation(delta=-26 * 3600)
    elif fault == 'shape':
        objects, _ = generation(change={'origins': []})
    elif fault == 'binding':
        objects, _ = generation(manifest_change={'usage_summary_generated_at': 'different-generation'})
    else:
        objects, _ = generation()
    if fault == 'digest':
        objects[pull.SUMMARY_NAME] += b' '
    if fault == 'fetch':
        objects.pop(pull.SUMMARY_NAME)
    with pytest.raises(pull.ScramblerFeedPullError):
        pull.pull_summary_once(objects.__getitem__, destination, now_epoch=NOW)
    assert destination.read_bytes() == previous
    assert not list(tmp_path.glob('.summary-*'))


def test_slow_older_generation_cannot_replace_newer_reader_file(tmp_path):
    destination = tmp_path / 'summary.json'
    newest, newest_bytes = generation(delta=30)
    older, _ = generation()
    pull.pull_summary_once(newest.__getitem__, destination, now_epoch=NOW)
    with pytest.raises(pull.ScramblerFeedPullError, match='summary_generation_regressed'):
        pull.pull_summary_once(older.__getitem__, destination, now_epoch=NOW)
    assert destination.read_bytes() == newest_bytes


def test_destination_symlink_never_overwrites_its_target(tmp_path):
    original = tmp_path / 'original.json'
    original.write_bytes(b'preserved synthetic private data')
    destination = tmp_path / 'summary.json'
    destination.symlink_to(original)
    objects, _ = generation()
    with pytest.raises(pull.ScramblerFeedPullError, match='summary_destination_invalid'):
        pull.pull_summary_once(objects.__getitem__, destination, now_epoch=NOW)
    assert destination.is_symlink()
    assert original.read_bytes() == b'preserved synthetic private data'
