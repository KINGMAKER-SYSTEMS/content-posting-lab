"""Scrambler ON-path readiness: name every missing pilot setting (HIGH-1).

The Scrambler fails closed when any ON-path requirement is missing, which
looks exactly like "the pilot produces zero cuts". This module turns those
requirements into a list of named problems, read from the environment and the
files it points at. It never writes, never bootstraps the private history and
adds nothing to any HTTP response; it is used by

* the app startup log line (``log_startup_preflight``, cheap checks only), and
* ``python tools/scrambler_report.py --preflight`` (full checks, including a
  real parse of the originality feed), which exits non-zero on any problem, and
* ``python tools/scrambler_report.py --preflight --as-enabled``
  (``preflight_problems_as_enabled``: the same checks as if the switch were
  on, plus proof that the feed files came from one verified pull), the gate
  run before the pilot flip.

The pilot environment is documented in docs/SCRAMBLER.md ("Pilot environment").
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from services import scrambler as core
from services import scrambler_originality as originality

FEED_R2_BUCKET_ENV = "SCRAMBLER_FEED_R2_BUCKET"
FEED_R2_PREFIX_ENV = "SCRAMBLER_FEED_R2_PREFIX"
FEED_USAGE_FILE_NAME = "usage.jsonl"  # the name services/scrambler_feed_pull.py writes
HISTORY_PATH_ENV = "SCRAMBLER_HISTORY_PATH"
VOLUME_ENV = "RAILWAY_VOLUME_MOUNT_PATH"

# The roster integration_id the Worker sends as the page id: Notion-sourced
# pages are ``acct:`` + the handle slugged to [a-z0-9-] (services/notion_pages.py
# mint_integration_id: freddy.2am -> acct:freddy-2am); rail pages are
# ``acct:rail:<hex>``. The gate matches page ids EXACTLY, so any other form
# (a bare handle, a dotted handle, upper case) silently leaves the page OFF.
ROSTER_PAGE_ID_RE = re.compile(r"^acct:(?:[a-z0-9]+(?:-[a-z0-9]+)*|rail:[0-9a-f]+)$")


def _stale_hours(path: Path, now: float) -> float | None:
    try:
        return (now - path.stat().st_mtime) / 3600.0
    except OSError:
        return None


def preflight_problems(env: Mapping[str, str] | None = None, *, full: bool = True,
                       now_epoch: float | None = None) -> list[str]:
    """Every missing or unusable Scrambler ON-path setting, by name.

    Empty list = the pilot configuration is complete. ``full`` also parses
    the originality feed and validates an existing history file.
    """
    env = os.environ if env is None else env
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    problems: list[str] = []

    if str(env.get(core.SCRAMBLER_ENABLED_ENV, "")).strip() != "1":
        problems.append(f"{core.SCRAMBLER_ENABLED_ENV} must be 1")
    pages = sorted(core._page_list(env.get(core.SCRAMBLER_PAGES_ENV)))
    denied = core._page_list(env.get(core.SCRAMBLER_DENY_PAGES_ENV))
    if not pages:
        problems.append(f"{core.SCRAMBLER_PAGES_ENV} is empty (comma list of roster integration_ids)")
    for page in pages:
        if not ROSTER_PAGE_ID_RE.match(page):
            problems.append(
                f"{core.SCRAMBLER_PAGES_ENV} entry {page!r} is not a roster integration_id "
                "(acct:<handle with every non [a-z0-9] run as '-'>); the gate matches exactly")
        if page in denied:
            problems.append(f"{core.SCRAMBLER_PAGES_ENV} entry {page!r} is also in {core.SCRAMBLER_DENY_PAGES_ENV}")
    raw_share = env.get(core.SCRAMBLER_FAST_SHARE_ENV)
    if raw_share is not None and str(raw_share).strip():
        try:
            core._parse_share(str(raw_share).strip(), name="scrambler_fast_share")
        except ValueError:
            problems.append(f"{core.SCRAMBLER_FAST_SHARE_ENV} must be a number from 0 to 1")
    # Same entry split and share parser as the enqueue (``fast_share_for``):
    # a bad share here makes every enqueue for that page refuse (409).
    for page, share in core._page_fast_share_entries(env.get(core.SCRAMBLER_PAGE_FAST_SHARE_ENV)):
        try:
            core._parse_share(share, name="scrambler_page_fast_share")
        except ValueError:
            problems.append(
                f"{core.SCRAMBLER_PAGE_FAST_SHARE_ENV} entry {page!r} share {share!r} must be a "
                "number from 0 to 1 (enqueue for that page refuses)")
    if str(env.get(core.SCRAMBLER_WORKER_COMPAT_ENV, "")).strip() != "1":
        problems.append(f"{core.SCRAMBLER_WORKER_COMPAT_ENV} must be 1")

    # The originality feed: usage ledger plus its origins.json sibling.
    usage_raw = env.get(originality.ORIGINALITY_USAGE_PATH_ENV)
    stale_h = originality.USAGE_STALE_AFTER_S / 3600.0
    usage_ok = False
    if usage_raw is None or not str(usage_raw).strip():
        problems.append(f"{originality.ORIGINALITY_USAGE_PATH_ENV} is not set")
    else:
        usage = Path(str(usage_raw).strip())
        age = _stale_hours(usage, now)
        if age is None or not usage.is_file():
            problems.append(f"{originality.ORIGINALITY_USAGE_PATH_ENV} file is missing: {usage}")
        elif age > stale_h:
            problems.append(f"{originality.ORIGINALITY_USAGE_PATH_ENV} is stale: {usage} is {age:.1f} h old (limit {stale_h:.0f} h)")
        elif usage.stat().st_size > originality.USAGE_MAX_BYTES:
            problems.append(f"{originality.ORIGINALITY_USAGE_PATH_ENV} is too large: {usage}")
        else:
            usage_ok = True
        origins_raw = env.get(originality.ORIGINALITY_ORIGINS_PATH_ENV)
        origins = Path(origins_raw) if origins_raw else usage.parent / originality.ORIGINS_FILE_NAME
        origins_age = _stale_hours(origins, now)
        if origins_age is None or not origins.is_file():
            problems.append(f"origins map is missing: {origins} (the {originality.ORIGINS_FILE_NAME} beside the usage ledger)")
            usage_ok = False
        elif origins_age > stale_h:
            problems.append(f"origins map is stale: {origins} is {origins_age:.1f} h old (limit {stale_h:.0f} h)")
            usage_ok = False
    if full and usage_ok:
        try:
            originality.load_originality_index(
                str(usage_raw).strip(),
                origins_path=env.get(originality.ORIGINALITY_ORIGINS_PATH_ENV) or None,
                retired_path=env.get(originality.ORIGINALITY_RETIRED_PATH_ENV) or None,
                now_epoch=now)
        except originality.ScramblerOriginalityUnavailable as error:
            problems.append(f"originality feed does not load: {error}")

    # Durable history: SCRAMBLER_HISTORY_PATH or the Railway volume.
    history_raw = env.get(HISTORY_PATH_ENV)
    volume = env.get(VOLUME_ENV)
    if history_raw:
        history = Path(history_raw)
        if not history.is_absolute():
            problems.append(f"{HISTORY_PATH_ENV} must be an absolute path: {history_raw}")
            history = None
    elif volume and Path(volume).is_absolute():
        history = Path(volume) / core.SCRAMBLER_HISTORY_NAME
    else:
        problems.append(f"no durable history path: set {HISTORY_PATH_ENV} (under the volume) or {VOLUME_ENV}")
        history = None
    if history is not None:
        parent = history.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            problems.append(f"history directory is missing or not writable: {parent}")
        if volume and Path(volume).is_absolute():
            try:
                history.resolve().relative_to(Path(volume).resolve())
            except ValueError:
                problems.append(f"history path {history} is not on the durable volume {volume}")
        if full and history.is_file():
            try:
                raw = history.read_bytes()
                if len(raw) > core.SCRAMBLER_HISTORY_MAX_BYTES:
                    raise core.ScramblerHistoryCorrupt("scrambler_history_bytes_exceeded")
                core._validate_history(json.loads(raw), path=history)
            except (OSError, ValueError, core.ScramblerHistoryCorrupt) as error:
                problems.append(f"history file does not validate: {history}: {error}")

    # The scheduled feed pull (services/scrambler_feed_pull.py) reads these;
    # without them the feed goes stale in 26 h.
    for name in (FEED_R2_BUCKET_ENV, FEED_R2_PREFIX_ENV):
        if not str(env.get(name, "")).strip():
            problems.append(f"{name} is not set (nightly originality feed pull)")
    # The pull writes usage.jsonl and origins.json into the usage ledger's
    # directory. A ledger with another file name, or an origins override that
    # points elsewhere, would make the loader read files the pull never
    # refreshes (and ON would refuse as stale within 26 h).
    if usage_raw is not None and str(usage_raw).strip():
        usage_path = Path(str(usage_raw).strip())
        if usage_path.name != FEED_USAGE_FILE_NAME:
            problems.append(
                f"{originality.ORIGINALITY_USAGE_PATH_ENV} must end in "
                f"/{FEED_USAGE_FILE_NAME}: the feed pull writes that name")
        origins_override = str(env.get(originality.ORIGINALITY_ORIGINS_PATH_ENV, "")).strip()
        if origins_override and Path(origins_override) != usage_path.parent / originality.ORIGINS_FILE_NAME:
            problems.append(
                f"{originality.ORIGINALITY_ORIGINS_PATH_ENV} must be unset or "
                f"{usage_path.parent / originality.ORIGINS_FILE_NAME}: the feed pull "
                "writes origins.json beside the usage ledger")
    return problems


FEED_MANIFEST_FILE_NAME = "manifest.json"  # written last by every good feed pull
FEED_MANIFEST_MAX_BYTES = 1024 * 1024


def pulled_feed_problems(dest: Path) -> tuple[object | None, list[str]]:
    """Does ``dest`` hold one complete, verified feed pull?

    Reads ``dest/manifest.json`` (the pull writes it last) and checks that
    usage.jsonl and origins.json beside it hash to the manifest's ``sha256s``
    and that the usage row count matches ``rows``. Returns ``(manifest or None,
    problems)``. Read-only; nothing is written or fetched.
    """
    from services import scrambler_feed_pull as feed  # local: no new import at startup

    manifest_path = dest / FEED_MANIFEST_FILE_NAME
    try:
        with manifest_path.open("rb") as stream:
            raw = stream.read(FEED_MANIFEST_MAX_BYTES + 1)
    except OSError:
        return None, [f"pulled feed manifest is missing: {manifest_path} "
                      "(run tools/scrambler_report.py --pull-feed-once)"]
    if len(raw) > FEED_MANIFEST_MAX_BYTES:
        return None, [f"pulled feed manifest is too large: {manifest_path}"]
    try:
        manifest = feed.parse_manifest(raw)
    except feed.ScramblerFeedPullError as error:
        return None, [f"pulled feed manifest does not parse: {manifest_path}: {error.reason}"]
    problems: list[str] = []
    for name in feed.DATA_FILES:
        path = dest / name
        digest = manifest.sha256s.get(name)
        try:
            data = path.read_bytes() if path.stat().st_size <= originality.USAGE_MAX_BYTES else None
        except OSError:
            problems.append(f"pulled feed file is missing: {path}")
            continue
        if data is None:
            problems.append(f"pulled feed file is too large: {path}")
        elif digest is None or feed.sha256_hex(data) != digest:
            problems.append(f"pulled feed file does not match its manifest sha256: {path}")
        elif name == "usage.jsonl" and feed.count_usage_rows(data) != manifest.rows:
            problems.append(f"pulled feed file does not match its manifest rows: {path}")
    return manifest, problems


def preflight_problems_as_enabled(env: Mapping[str, str] | None = None, *,
                                  now_epoch: float | None = None) -> list[str]:
    """Every ON-path problem as if ``SCRAMBLER_ENABLED=1``, plus proof of a pull.

    The pilot-flip gate: run BEFORE the master switch is set. It evaluates
    ``preflight_problems(full=True)`` on a COPY of the environment with the
    switch forced to 1 (the process environment is never changed), then also
    requires that the feed files beside the usage ledger came from one verified
    pull (``manifest.json`` present, its sha256s and rows match, and its
    ``generated_at`` is at most 20 h old, the pull's own staleness bar). So it
    fails until ``--pull-feed-once`` worked and every other setting is in place.
    """
    from services import scrambler_feed_pull as feed  # local: no new import at startup

    env = dict(os.environ if env is None else env)
    env[core.SCRAMBLER_ENABLED_ENV] = "1"
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    problems = preflight_problems(env, full=True, now_epoch=now)
    usage_raw = str(env.get(originality.ORIGINALITY_USAGE_PATH_ENV, "")).strip()
    if usage_raw:
        manifest, pulled = pulled_feed_problems(Path(usage_raw).parent)
        problems.extend(pulled)
        if manifest is not None:
            epoch = manifest.generated_at_epoch
            limit_h = feed.MANIFEST_STALE_AFTER_S / 3600.0
            if epoch is None:
                problems.append("pulled feed manifest generated_at is not an ISO 8601 time")
            elif now - epoch > feed.MANIFEST_STALE_AFTER_S:
                problems.append(
                    f"pulled feed manifest is stale: generated_at {manifest.generated_at} is "
                    f"{(now - epoch) / 3600.0:.1f} h old (limit {limit_h:.0f} h); "
                    "the originality producer has not run")
    return problems


def log_startup_preflight(logger: logging.Logger | None = None,
                          env: Mapping[str, str] | None = None) -> list[str]:
    """One startup log line: OFF, ready, or NOT READY with every missing setting."""
    logger = logger or logging.getLogger("scrambler.preflight")
    env = os.environ if env is None else env
    if (str(env.get(core.SCRAMBLER_ENABLED_ENV, "")).strip() != "1"
            and not core._page_list(env.get(core.SCRAMBLER_PAGES_ENV))):
        logger.info("scrambler preflight: OFF")
        return []
    problems = preflight_problems(env, full=False)
    if problems:
        logger.warning("scrambler preflight: NOT READY (%d): %s", len(problems), "; ".join(problems))
    else:
        logger.info("scrambler preflight: ready pages=%s",
                    ",".join(sorted(core._page_list(env.get(core.SCRAMBLER_PAGES_ENV)))))
    return problems
