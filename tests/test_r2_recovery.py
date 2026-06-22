"""Error-recovery tests for services/r2.py.

r2.py wraps a boto3 S3 client. The module's *own* logic — the branches that
aren't just a passthrough to boto3 — is what these tests pin down:

  - auth/config failures surface as R2NotConfigured (and is_configured() == False)
  - head() swallows the 404 family but re-raises everything else (auth, timeout)
  - delete_many() handles the empty batch and partial-failure (Errors) cases
  - delete_prefix() walks pagination and tolerates an empty bucket
  - list/count account objects paginate, skip the .placeholder marker, and
    honour the max_keys ceiling
  - transient transport errors (ConnectTimeout) propagate rather than being
    silently eaten

Everything is exercised against a fake S3 client injected via r2._CLIENT, so no
network and no real credentials are touched.
"""

from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

import services.r2 as r2


@pytest.fixture(autouse=True)
def _bucket_and_clean(monkeypatch):
    """Set a bucket and reset the cached client around every test."""
    monkeypatch.setenv("R2_BUCKET", "test-bucket")
    r2._CLIENT = None
    yield
    r2._CLIENT = None


def _inject(fake):
    """Pin a fake client so r2.client() returns it without env/boto3."""
    r2._CLIENT = fake
    return fake


def _client_error(code, op="HeadObject"):
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, op)


class FakeS3:
    """Minimal S3 stand-in. Each method either returns a queued response or
    raises a queued exception, so we can script timeouts/auth failures."""

    def __init__(self):
        self.list_pages = []     # list of list_objects_v2 responses
        self.deleted = []        # batches passed to delete_objects
        self.delete_resp = {}
        self.head_error = None
        self.head_resp = {"ContentLength": 0}
        self.put_calls = []

    def list_objects_v2(self, **kwargs):
        if not self.list_pages:
            return {"Contents": [], "IsTruncated": False}
        return self.list_pages.pop(0)

    def delete_objects(self, **kwargs):
        self.deleted.append(kwargs["Delete"]["Objects"])
        return self.delete_resp

    def head_object(self, **kwargs):
        if self.head_error:
            raise self.head_error
        return self.head_resp

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {}


# ── config / auth failures ───────────────────────────────────────────────


def test_missing_creds_raises_not_configured(monkeypatch):
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    r2._CLIENT = None
    with pytest.raises(r2.R2NotConfigured):
        r2.client()
    assert r2.is_configured() is False


def test_missing_bucket_raises(monkeypatch):
    monkeypatch.delenv("R2_BUCKET", raising=False)
    with pytest.raises(r2.R2NotConfigured):
        r2._bucket()


# ── head(): 404 family swallowed, auth/timeout re-raised ─────────────────


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_head_returns_none_on_not_found(code):
    fake = _inject(FakeS3())
    fake.head_error = _client_error(code)
    assert r2.head("missing/key") is None


def test_head_reraises_auth_error():
    fake = _inject(FakeS3())
    fake.head_error = _client_error("403")  # auth failure, not a miss
    with pytest.raises(ClientError):
        r2.head("some/key")


def test_head_reraises_timeout():
    fake = _inject(FakeS3())
    fake.head_error = ConnectTimeoutError(endpoint_url="https://r2.example")
    with pytest.raises(ConnectTimeoutError):
        r2.head("some/key")


# ── delete_many(): empty + partial-failure accounting ────────────────────


def test_delete_many_empty_is_noop():
    fake = _inject(FakeS3())
    assert r2.delete_many([]) == 0
    assert fake.deleted == []  # never hit the network


def test_delete_many_counts_partial_failures():
    fake = _inject(FakeS3())
    fake.delete_resp = {"Errors": [{"Key": "b", "Code": "AccessDenied"}]}
    # 3 requested, 1 failed → 2 deleted
    assert r2.delete_many(["a", "b", "c"]) == 2


def test_delete_many_all_succeed():
    fake = _inject(FakeS3())
    fake.delete_resp = {}  # no Errors key
    assert r2.delete_many(["a", "b"]) == 2


# ── delete_prefix(): pagination + empty bucket ───────────────────────────


def test_delete_prefix_empty_bucket():
    _inject(FakeS3())  # default list returns empty/untruncated
    assert r2.delete_prefix("clips/none/") == 0


def test_delete_prefix_walks_pages():
    fake = _inject(FakeS3())
    fake.list_pages = [
        {"Contents": [{"Key": "p/1"}, {"Key": "p/2"}], "IsTruncated": True,
         "NextContinuationToken": "tok"},
        {"Contents": [{"Key": "p/3"}], "IsTruncated": False},
    ]
    assert r2.delete_prefix("p/") == 3
    # both pages' keys were submitted for deletion
    assert [o["Key"] for batch in fake.deleted for o in batch] == ["p/1", "p/2", "p/3"]


# ── list/count account objects: placeholder skip, pagination, ceiling ────


def test_list_account_objects_skips_placeholder():
    fake = _inject(FakeS3())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake.list_pages = [{
        "Contents": [
            {"Key": "accounts/x/.placeholder", "Size": 0},
            {"Key": "accounts/x/clip.mp4", "Size": 10, "LastModified": now},
        ],
        "IsTruncated": False,
    }]
    items = r2.list_account_objects("x")
    assert [i["filename"] for i in items] == ["clip.mp4"]
    assert items[0]["last_modified"] == now.isoformat()


def test_list_account_objects_empty():
    _inject(FakeS3())
    assert r2.list_account_objects("nobody") == []


def test_count_account_objects_paginates_and_skips_placeholder():
    fake = _inject(FakeS3())
    fake.list_pages = [
        {"Contents": [{"Key": "accounts/x/.placeholder"}, {"Key": "accounts/x/a"}],
         "IsTruncated": True, "NextContinuationToken": "t"},
        {"Contents": [{"Key": "accounts/x/b"}], "IsTruncated": False},
    ]
    assert r2.count_account_objects("x") == 2  # placeholder not counted


def test_list_account_objects_honours_max_keys_ceiling():
    fake = _inject(FakeS3())
    # Page is truncated but we've already hit max_keys → loop must stop.
    fake.list_pages = [
        {"Contents": [{"Key": "accounts/x/a", "Size": 1},
                      {"Key": "accounts/x/b", "Size": 1}],
         "IsTruncated": True, "NextContinuationToken": "t"},
        {"Contents": [{"Key": "accounts/x/c", "Size": 1}], "IsTruncated": False},
    ]
    items = r2.list_account_objects("x", max_keys=2)
    assert len(items) == 2  # stopped before fetching the second page
    assert fake.list_pages == [{"Contents": [{"Key": "accounts/x/c", "Size": 1}],
                                "IsTruncated": False}]  # second page untouched


# ── create_account_prefix(): idempotent marker drop ──────────────────────


def test_create_account_prefix_idempotent_when_marker_exists():
    fake = _inject(FakeS3())
    fake.head_resp = {"ContentLength": 0}  # marker already there
    out = r2.create_account_prefix("acct1")
    assert out["prefix"] == "accounts/acct1/"
    assert fake.put_calls == []  # did not re-create


def test_create_account_prefix_drops_marker_when_missing():
    fake = _inject(FakeS3())
    fake.head_error = _client_error("404")  # marker missing
    r2.create_account_prefix("acct2")
    assert len(fake.put_calls) == 1
    assert fake.put_calls[0]["Key"] == "accounts/acct2/.placeholder"
