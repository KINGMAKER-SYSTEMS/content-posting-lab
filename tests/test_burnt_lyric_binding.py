"""Burnt's historical timed-lyric binding is retired, not executable."""
import hashlib
import json

import pytest

from services.control_plane_lyric_video import managed_lyric_binding, validate_managed_lyric_job
from tests.test_control_plane_lyric_video import PAGE, spec


def fixture():
    value = spec()
    raw = json.dumps(value, separators=(",", ":"))
    publication = {
        "pageId": PAGE, "recipeId": "lyric-video:master", "engine": "lyrics_slideshows",
        "recipeVersion": "fixture-reviewed-version", "recipeSpecCanonical": raw,
        "recipeSpecHash": hashlib.sha256(raw.encode()).hexdigest(),
    }
    job = {key: publication[key] for key in ("pageId", "recipeId", "engine", "recipeVersion", "recipeSpecHash")}
    job.update(jobId="test-only-retired-lyric-job", sourceKind="syzygy_slideshow", status="completed")
    return value, publication, job


def test_retired_burnt_lyric_job_cannot_bind_or_serve_a_historical_clip():
    value, publication, job = fixture()
    reason = "burnt lyric video disabled: static captions only"
    with pytest.raises(ValueError, match=reason):
        validate_managed_lyric_job(job, publication, value)
    with pytest.raises(ValueError, match=reason):
        managed_lyric_binding(job, publication, value, "a" * 64)
