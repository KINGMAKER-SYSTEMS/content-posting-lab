import json
import sqlite3
from pathlib import Path

import pytest

from services.post_render_jobs import PostRenderJobs, RenderJobError, PEAK_RENDER_WORKSPACE_BYTES
from services.post_render import sha256
from tests.test_post_render_jobs import service, submission
from tests.test_post_render_dual_dedup import dual_render, payload


def test_finish_identity_probe_does_not_read_history_json(tmp_path):
    worker = service(tmp_path)
    job = worker.enqueue(submission(), 'request:probe')
    assert worker.run_one()
    row = worker._row(job['id'])
    authority = json.loads(row['output_authority_json'])
    with worker._db() as db:
        statements = []
        db.set_trace_callback(statements.append)
        worker._reserve_output_identity(db, row, authority)
    assert not any('SELECT' in sql.upper() and 'output_authority_json' in sql for sql in statements)
    for sql in statements:
        if sql.startswith('SELECT'):
            with worker._db() as db:
                plan = db.execute('EXPLAIN QUERY PLAN ' + sql).fetchall()
            assert all('SCAN' not in part[3] for part in plan)


def test_old_shared_retired_identity_is_backfilled_without_schema_change(tmp_path):
    worker = service(tmp_path, renderer=dual_render)
    for i, caption in enumerate(('one', 'two')):
        job = worker.enqueue(payload(f'slot:{i}', caption), f'request:{i}')
        assert worker.run_one()
        worker.acknowledge(job['id'])
        worker.retire(job['id'])
    with worker._db() as db:
        schema = list(db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
        # Simulate 248: it knows only public idempotency keys, not this index.
        db.execute("DELETE FROM idempotency WHERE key LIKE '/output-identity/%'")
    worker = service(tmp_path, renderer=dual_render)
    with worker._db() as db:
        assert [tuple(r) for r in schema] == [tuple(r) for r in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
        assert db.execute("SELECT COUNT(*) FROM idempotency WHERE key LIKE '/output-identity/%'").fetchone()[0] == 2
    job = worker.enqueue(payload('slot:repeated', 'two'), 'request:repeated')
    assert worker.run_one()
    assert worker.status(job['id'])['error_code'] == 'duplicate_output'


def test_known_identity_survives_later_authority_corruption(tmp_path, caplog):
    worker = service(tmp_path, renderer=dual_render)
    job = worker.enqueue(payload('slot:first', 'same'), 'request:first')
    assert worker.run_one()
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json='{' WHERE id=?", (job['id'],))
    worker = service(tmp_path, renderer=dual_render)
    assert job['id'] in caplog.text
    again = worker.enqueue(payload('slot:again', 'same'), 'request:again')
    assert worker.run_one()
    assert worker.status(again['id'])['error_code'] == 'duplicate_output'


def test_internal_reservations_cannot_be_used_as_public_request_keys(tmp_path):
    worker = service(tmp_path)
    with pytest.raises(RenderJobError, match='invalid_idempotency_key'):
        worker.enqueue(submission(), '/output-identity/v1/' + 'a' * 64)


def test_workspace_docs_match_actual_worker_reserve():
    text = (Path(__file__).parents[1] / 'docs/post-render-setup.md').read_text()
    assert text.count(f'{PEAK_RENDER_WORKSPACE_BYTES:,}') >= 2
    assert '1,140,151,642' not in text


def test_renderer_describes_pair_verification_without_filesystem_atomicity_claim():
    from services.post_render import render_post
    assert 'atomically' not in render_post.__doc__
    assert 'verif' in render_post.__doc__


def test_deeply_nested_corrupt_authority_does_not_stop_startup(tmp_path, caplog):
    worker = service(tmp_path)
    bad = worker.enqueue(submission(slot_id='slot:deep'), 'request:deep')
    with worker._db() as db:
        db.execute('UPDATE jobs SET output_authority_json=?,state=? WHERE id=?',
                   ('[' * 2000 + '0' + ']' * 2000, 'succeeded', bad['id']))
    worker = service(tmp_path)
    good = worker.enqueue(submission(slot_id='slot:afterdeep'), 'request:afterdeep')
    assert worker.run_one()
    assert worker.status(good['id'])['state'] == 'succeeded'
    assert bad['id'] in caplog.text
