"""Scrambler ON-path readiness: name every missing pilot setting (HIGH-1).

The Scrambler fails closed when any ON-path requirement is missing, which
looks exactly like "the pilot produces zero cuts". This module turns those
requirements into a list of named problems, read from the environment and the
files it points at. It never writes, never bootstraps the private history and
adds nothing to any HTTP response; it is used by

* a bounded background startup observation (``run_startup_preflight``), and
* ``python tools/scrambler_report.py --preflight`` (full checks, including a
  real parse of the originality feed), which exits non-zero on any problem, and
* ``python tools/scrambler_report.py --preflight --as-enabled``
  (``preflight_problems_as_enabled``: the same checks as if the switch were
  on, plus proof that the feed files came from one verified pull), the gate
  run before the pilot flip.

The pilot environment is documented in docs/SCRAMBLER.md ("Pilot environment").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
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
STARTUP_DIAGNOSTIC_TIMEOUT_S = 10.0


async def run_startup_preflight() -> None:
    """Observe pilot readiness without blocking startup or retaining stuck I/O.

    Resolution may read a remote manifest. A thread timeout cannot stop that
    read, so reuse the owned process lifecycle and reap it at the total deadline
    or on app shutdown. This is diagnostic only: enqueue checks remain intact.
    """
    from services.owned_import_process import create_import_subprocess

    logger = logging.getLogger("scrambler.preflight")
    deadline = asyncio.get_running_loop().time() + STARTUP_DIAGNOSTIC_TIMEOUT_S
    try:
        async with asyncio.timeout_at(deadline):
            process = await create_import_subprocess(
                sys.executable, "-m", "services.scrambler_preflight",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            output, _ = await process.communicate()
        if process.returncode:
            logger.warning("scrambler preflight: diagnostic process failed (exit=%s)",
                           process.returncode)
        else:
            # Keep child diagnostic output bounded; no new HTTP/status fields.
            if len(output) > 8192:
                logger.warning("scrambler preflight: diagnostic output truncated")
            for line in output.decode("utf-8", errors="replace")[-8192:].splitlines():
                if line.startswith("scrambler preflight:"):
                    logger.log(logging.WARNING if "NOT READY" in line else logging.INFO,
                               "%s", line)
    except TimeoutError:
        logger.warning("scrambler preflight: readiness unknown (diagnostic deadline exceeded)")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("scrambler preflight: readiness unknown (diagnostic %s)",
                       type(error).__name__)

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


def _resolve_page_master_shas(page: str) -> tuple[list[str], str | None]:
    """Resolve ``page``'s current master SHAs the way the planner does, plus a
    short failure reason (``None`` on success).

    Roster intent -> the page's current registered recipe bindings ->
    ``resolve_source_recipe`` -> its master library. Returns ``(shas, failure)``:
    on success ``shas`` is non-empty and ``failure`` is ``None``; any failure to
    resolve (missing intent, no matching bindings, an unresolvable recipe, or an
    empty master library) answers ``([], reason)`` so the caller fails CLOSED
    instead of silently reporting the page ready.

    Bounded and fail-soft by design (LOW-2): this runs once at boot, iterates at
    most one ``resolve_source_recipe`` call per registered binding (pages x
    bindings), and each ShipStream manifest fetch is a single best-effort read
    with no retry. A vault/network failure raises ``ShipStreamSourceUnavailable``,
    which ``resolve_source_recipe`` already converts to ``None`` (never a crash);
    we answer ``([], reason)`` and the preflight logs NOT READY.
    """
    from routers.control_plane import _current_master_pages_intent
    from routers.control_plane_recipes import list_registered_recipe_bindings
    from services.control_plane_sources import resolve_source_recipe

    try:
        intent = _current_master_pages_intent(page)
    except Exception as error:  # fail closed: a broken seam must not read ready
        return [], f"intent_exception:{type(error).__name__}"
    if intent is None:
        return [], "no_master_pages_intent"
    master_pages, master_pages_hash = intent
    try:
        bindings = list_registered_recipe_bindings(
            page, master_pages, master_pages_hash)
    except Exception as error:
        return [], f"bindings_exception:{type(error).__name__}"
    if not bindings:
        return [], "no_registered_recipe_bindings"
    shas: list[str] = []
    for _bound_page, publication in bindings:
        try:
            recipe = resolve_source_recipe(publication)
        except Exception as error:
            return [], f"resolve_exception:{type(error).__name__}"
        if recipe is None:
            return [], "recipe_unresolved"
        shas.extend(master.sha256 for master in getattr(recipe, "masters", ()))
    if not shas:
        return [], "empty_masters"
    return shas, None


def preflight_page_origin_problems(env: Mapping[str, str] | None = None) -> list[str]:
    """One ``page=<id> reason=<r>`` per listed page that is not READY.

    Two failure classes, both named by page (MED-1):

    * ``origin_mapping_unknown:<sha12>`` / ``origin_window_unknown:<kind>:<id>``
      — the page resolves to masters but a master lacks a usable origin mapping;
    * ``master_resolution_failed:<short>`` — a listed page cannot be resolved to
      masters at all (no intent, no bindings, unresolvable recipe, or empty
      masters). This used to answer ``[]`` and read ``ready``; it now fails
      closed so a resolution drift is visible at startup.

    This is the startup visibility for the 409 the enqueue would otherwise
    refuse with (``scrambler_unavailable: origin_mapping_unknown:<sha12>``).
    """
    env = os.environ if env is None else env
    pages = sorted(core._page_list(env.get(core.SCRAMBLER_PAGES_ENV)))
    if not pages:
        return []
    usage_raw = str(env.get(originality.ORIGINALITY_USAGE_PATH_ENV, "")).strip()
    if not usage_raw:
        return []
    usage = Path(usage_raw)
    origins_raw = str(env.get(originality.ORIGINALITY_ORIGINS_PATH_ENV, "")).strip()
    origins = Path(origins_raw) if origins_raw else usage.parent / originality.ORIGINS_FILE_NAME
    try:
        origins_map = originality._load_origins_map(origins)
    except originality.ScramblerOriginalityUnavailable as error:
        # A missing map is also reported by preflight_problems(full=False), but
        # an unparseable/corrupt map is only parsed by the full check — startup
        # would otherwise log ready. Either way, without a usable map no
        # master's origin can be verified, so fail CLOSED and name every page.
        short = str(error).split(":", 1)[0] or "origins_map_unavailable"
        return [f"page={page} reason=origins_map_unavailable:{short}"
                for page in pages]
    problems: list[str] = []
    for page in pages:
        try:
            shas, resolution_failure = _resolve_page_master_shas(page)
        except Exception as error:
            shas, resolution_failure = [], f"exception:{type(error).__name__}"
        if resolution_failure:
            problems.append(
                f"page={page} reason=master_resolution_failed:{resolution_failure}")
            continue
        reason = None
        for sha in shas:
            key = originality._normalize_master_sha(sha)
            if key is None:
                reason = f"origin_mapping_unknown:{str(sha)[:12]}"
            else:
                mapping = origins_map.get(key)
                if mapping is None:
                    reason = f"origin_mapping_unknown:{key[:12]}"
                elif mapping.window is None:
                    reason = (f"origin_window_unknown:{mapping.origin_kind}:"
                              f"{mapping.origin_id}")
            if reason:
                break
        if reason:
            problems.append(f"page={page} reason={reason}")
    return problems


def log_startup_preflight(logger: logging.Logger | None = None,
                          env: Mapping[str, str] | None = None) -> list[str]:
    """One startup log line: OFF, ready, or NOT READY with every missing setting.

    A listed page that cannot be resolved to masters, or whose current master(s)
    lack a usable origin mapping, is logged by name and is NOT reported ready.
    """
    logger = logger or logging.getLogger("scrambler.preflight")
    env = os.environ if env is None else env
    if (str(env.get(core.SCRAMBLER_ENABLED_ENV, "")).strip() != "1"
            and not core._page_list(env.get(core.SCRAMBLER_PAGES_ENV))):
        logger.info("scrambler preflight: OFF")
        return []
    problems = preflight_problems(env, full=False)
    problems.extend(preflight_page_origin_problems(env))
    if problems:
        for problem in problems:
            if problem.startswith("page="):
                logger.warning("scrambler preflight: NOT READY %s", problem)
        logger.warning("scrambler preflight: NOT READY (%d): %s", len(problems), "; ".join(problems))
    else:
        logger.info("scrambler preflight: ready pages=%s",
                    ",".join(sorted(core._page_list(env.get(core.SCRAMBLER_PAGES_ENV)))))
    return problems


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log_startup_preflight()
