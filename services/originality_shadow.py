"""The originality shadow over the source cut planner (gap G39; shadow only).

`plan_source_cuts` (services/control_plane_sources.py) keeps choosing exactly
the same windows it always chose; this module only LOGS every chosen window
that would reuse footage the network has already shown, according to the
originality ledger's daily usage summary:

    {generated_at, origins: {origin_id: {duration_s?, seconds:
        [[start_s, end_s, page, shows, last_shown_at], ...]}},
     masters: {master_sha: {origin_id, offset_s}}}

Source (originality + pixel89 privacy ruling 2026-10-28 21:1x): the summary
maps EVERY page handle to its footage, so it is private data and is NEVER
fetched from a public URL. The primary source is a FILE selected by env
CONTENT_LAB_ORIGINALITY_SUMMARY_PATH; the shadow phase's default location is
REPO_SUMMARY_PATH (recipes/originality/usage-summary.json in this private
repo, refreshed by commit; originality hands a fresh copy at deploy time).
A URL source (env CONTENT_LAB_ORIGINALITY_SUMMARY_URL) is honoured ONLY
together with an auth header from env CONTENT_LAB_ORIGINALITY_SUMMARY_TOKEN
(never logged): a URL without a token never fetches anything — the summary
reads as unavailable, logged once, and planning proceeds. With neither env
set the feature is fully off: zero file reads, zero network calls, zero
logs, zero counters.

Rules (zcode/ORIGINALITY-GATE-DESIGN-1028.md (b); constants below):

- `cross_page`: any overlapping origin second shown on ANOTHER page within
  CROSS_PAGE_DAYS (90).
- `shows_cap`: the same page already showed a covered second
  ORIGIN_SECOND_SHOWS_PER_PAGE (2) times within ORIGIN_SECOND_COOLDOWN_DAYS
  (30).
- `gap_lt_7d`: the same page showed a covered second less than
  ORIGIN_SECOND_MIN_GAP_DAYS (7) ago.
- `unknown_origin`: the cut's origin cannot be named (no library origin, no
  summary masters row) while the summary itself is available.

Windows overlap when they share any origin second (WINDOW_OVERLAP_MAX_S = 0);
windows that merely touch are distinct (WINDOW_DISTINCT_MIN_GAP_S = 0). A
window is speed-adjusted by construction: the planner already plans source
windows, so a 0.5x delivered 9 s cut is a 4.5 s window here and covers 4.5
origin seconds. A master whose origin start is unknown touches the WHOLE
origin (0..duration_s, unbounded when duration_s is missing too).

Never refuses, never blocks, never changes a plan: every failure mode (read
error, fetch error, parse error, summary older than SUMMARY_MAX_AGE_H) makes
the summary "unavailable", logged ONCE per cache period (and counted), and
planning proceeds untouched. Every source is bounded (file stat size + read
cap or timeout <= SUMMARY_TIMEOUT_S; size cap SUMMARY_MAX_BYTES) and cached
in-process for SUMMARY_CACHE_S, re-read earlier when the file's mtime moves
(a commit-refreshed file is the norm). Inside a running event loop the shadow
never reads or fetches (that would block the ASGI loop for up to the
timeout): a cold cache there reads as unavailable, logged ONCE per cache
period instead of silently, and the capabilities poll (which runs in a
threadpool) keeps the cache warm. The generated_at > SUMMARY_MAX_AGE_H gate
is re-applied on every cached return, so an expired summary never comes back
as a `_Ready`.
"""
from __future__ import annotations

import asyncio
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import os
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger("content_lab.originality_shadow")

SUMMARY_PATH_ENV = "CONTENT_LAB_ORIGINALITY_SUMMARY_PATH"
SUMMARY_URL_ENV = "CONTENT_LAB_ORIGINALITY_SUMMARY_URL"
SUMMARY_TOKEN_ENV = "CONTENT_LAB_ORIGINALITY_SUMMARY_TOKEN"
# The shadow phase's default deploy location (set CONTENT_LAB_ORIGINALITY_
# SUMMARY_PATH to this): this PRIVATE repo, refreshed by commit, never a
# public URL (privacy ruling 2026-10-28 21:1x).
REPO_SUMMARY_PATH = "recipes/originality/usage-summary.json"
SUMMARY_TIMEOUT_S = 3.0
SUMMARY_MAX_BYTES = 8 * 1024 * 1024
SUMMARY_CACHE_S = 15 * 60.0
SUMMARY_MAX_AGE_H = 26.0
# ORIGINALITY-GATE-DESIGN-1028.md (b): the final constants.
ORIGIN_SECOND_SHOWS_PER_PAGE = 2
ORIGIN_SECOND_COOLDOWN_DAYS = 30
ORIGIN_SECOND_MIN_GAP_DAYS = 7
CROSS_PAGE_DAYS = 90
WINDOW_OVERLAP_MAX_S = 0.0
WINDOW_DISTINCT_MIN_GAP_S = 0.0

CROSS_PAGE = "cross_page"
SHOWS_CAP = "shows_cap"
GAP_LT_7D = "gap_lt_7d"
UNKNOWN_ORIGIN = "unknown_origin"
REASONS = (CROSS_PAGE, SHOWS_CAP, GAP_LT_7D, UNKNOWN_ORIGIN)

_DAY_S = 86_400.0


class SummaryUnavailable(Exception):
    """The usage summary cannot be used right now (fetch, parse or age)."""


@dataclass
class _OriginIndex:
    """One origin's shown seconds, queryable by overlapping window."""

    duration_s: float = math.inf
    starts: list[float] = field(default_factory=list)
    ends: list[float] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    shows: list[int] = field(default_factory=list)
    last_shown: list[float] = field(default_factory=list)
    prefix_max_end: list[float] = field(default_factory=list)

    def overlapping(self, start: float, end: float, now: float):
        """Rows sharing any second with [start, end): (row, age_s) pairs."""
        # Rows are sorted by start; candidates have start < end, and walking
        # back stops once no earlier row can still reach past `start`.
        index = bisect_left(self.starts, end)
        for position in range(index - 1, -1, -1):
            if self.prefix_max_end[position] <= start:
                break
            if self.ends[position] > start:
                row_start = self.starts[position]
                yield (
                    (row_start, self.ends[position], self.pages[position],
                     self.shows[position], self.last_shown[position]),
                    now - self.last_shown[position],
                )


@dataclass
class _Ready:
    generated_at: str
    generated_at_ts: float
    fetched_at: float
    origins: dict[str, _OriginIndex]
    masters: dict[str, dict[str, Any]]


@dataclass
class _Unavailable:
    reason: str
    logged: bool = False


def _now() -> float:
    return time.time()


def summary_path() -> str:
    """The configured summary file path, or '' when no file source is set."""
    value = os.environ.get(SUMMARY_PATH_ENV, "")
    return value.strip() if isinstance(value, str) else ""


def summary_url() -> str:
    """The configured summary URL, or '' when no URL source is set."""
    value = os.environ.get(SUMMARY_URL_ENV, "")
    return value.strip() if isinstance(value, str) else ""


def summary_token() -> str:
    """The auth token for a URL source (never logged), '' when unset."""
    value = os.environ.get(SUMMARY_TOKEN_ENV, "")
    return value.strip() if isinstance(value, str) else ""


def _summary_source() -> tuple[str, str, str]:
    """(kind, location, token): how this process reads the usage summary.

    'file' wins over 'url' (the private repo file is the preferred source);
    '' means the feature is fully off. A URL without a token stays 'url' but
    never fetches: the summary maps every page handle to its footage, so an
    unauthenticated public fetch is exactly what the 2026-10-28 21:1x
    originality + pixel89 ruling forbids — it reads as unavailable instead.
    """
    path = summary_path()
    if path:
        return "file", path, ""
    url = summary_url()
    if url:
        return "url", url, summary_token()
    return "", "", ""


def _mtime(location: str) -> float | None:
    """The file's mtime, or None when it cannot be stated."""
    try:
        return os.stat(location).st_mtime
    except OSError:
        return None


def _read_summary_file(path: str) -> bytes:
    """Read the summary file, house style: size-capped by stat BEFORE any
    read, then a hard read cap in case the file grew in between."""
    try:
        if os.stat(path).st_size > SUMMARY_MAX_BYTES:
            raise SummaryUnavailable("summary too large")
        with open(path, "rb") as handle:
            chunks = handle.read(SUMMARY_MAX_BYTES + 1)
    except SummaryUnavailable:
        raise
    except OSError as error:
        raise SummaryUnavailable("summary read failed") from error
    if len(chunks) > SUMMARY_MAX_BYTES:
        raise SummaryUnavailable("summary too large")
    return chunks


def _fetch_summary(url: str, token: str) -> bytes:
    """GET the summary with the auth header, house style: bounded timeout and
    body size. The token travels only in this header, never into a log."""
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"accept": "application/json", "authorization": f"Bearer {token}"},
            timeout=SUMMARY_TIMEOUT_S,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            advertised = response.headers.get("content-length")
            if advertised is not None:
                try:
                    if int(advertised) > SUMMARY_MAX_BYTES:
                        raise SummaryUnavailable("summary too large")
                except ValueError as error:
                    raise SummaryUnavailable("invalid content length") from error
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > SUMMARY_MAX_BYTES:
                    raise SummaryUnavailable("summary too large")
    except SummaryUnavailable:
        raise
    except httpx.HTTPError as error:
        raise SummaryUnavailable("summary fetch failed") from error
    return bytes(chunks)


def _parse_ts(value: Any) -> float | None:
    """Epoch seconds for an ISO timestamp, tolerating Z and -0400 offsets.

    None for garbage: a shadow log that cannot date a show treats it as
    recent (conservative), and a generated_at that cannot be dated makes the
    whole summary unavailable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _origin_index(value: Any) -> _OriginIndex:
    index = _OriginIndex()
    if not isinstance(value, dict):
        return index
    duration = value.get("duration_s")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
            and math.isfinite(float(duration)) and float(duration) > 0:
        index.duration_s = float(duration)
    rows = value.get("seconds")
    if not isinstance(rows, list):
        return index
    parsed = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 5:
            continue
        start, end, page, shows, last_shown = row
        last_shown_ts = _parse_ts(last_shown)
        if last_shown_ts is None:
            # Undatable shows read as shown right now: the shadow errs toward
            # logging, never toward silence.
            last_shown_ts = _now()
        if not isinstance(start, (int, float)) or isinstance(start, bool):
            continue
        if not isinstance(end, (int, float)) or isinstance(end, bool):
            continue
        if not isinstance(page, str) or not page:
            continue
        if not isinstance(shows, int) or isinstance(shows, bool) or shows < 0:
            continue
        parsed.append((float(start), float(end), page, int(shows), last_shown_ts))
    parsed.sort()
    running = -math.inf
    for start, end, page, shows, last_shown_ts in parsed:
        index.starts.append(start)
        index.ends.append(end)
        index.pages.append(page)
        index.shows.append(shows)
        index.last_shown.append(last_shown_ts)
        running = max(running, end)
        index.prefix_max_end.append(running)
    return index


def _parse_summary(raw: bytes, now: float) -> _Ready:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise SummaryUnavailable("summary is not JSON") from error
    if not isinstance(value, dict):
        raise SummaryUnavailable("summary is not an object")
    generated_at = _parse_ts(value.get("generated_at"))
    if generated_at is None:
        raise SummaryUnavailable("summary generated_at is invalid")
    if now - generated_at > SUMMARY_MAX_AGE_H * 3_600.0:
        raise SummaryUnavailable("summary is stale")
    origins_value = value.get("origins")
    masters_value = value.get("masters")
    if not isinstance(origins_value, dict) or not isinstance(masters_value, dict):
        raise SummaryUnavailable("summary shape is invalid")
    origins = {str(key): _origin_index(row) for key, row in origins_value.items()}
    masters = {
        str(key): row for key, row in masters_value.items() if isinstance(row, dict)
    }
    return _Ready(
        generated_at=value.get("generated_at"), generated_at_ts=generated_at,
        fetched_at=now, origins=origins, masters=masters,
    )


# In-process cache: one entry per source ("file:<path>" / "url:<url>"),
# replaced whole on every refresh. The middle term is the file mtime the
# entry was read at (None for URL sources): a moved mtime re-reads the file
# even inside the cache period.
_CACHE: dict[str, tuple[float, float | None, _Ready | _Unavailable]] = {}
# Off-loop refresh single-flight: one fetch per source; waiters block on the
# event until the owner writes the cache. _LOCK is never held during a fetch.
_FLIGHT: dict[str, threading.Event] = {}
# Last time (epoch s) an event-loop cold/expired cache miss was logged, per
# source key: the loop never fetches, but it still says so ONCE per cache
# period (and counts it) instead of silently.
_LOOP_MISS_LOGGED: dict[str, float] = {}
_COUNTS: dict[str, int] = {name: 0 for name in
                           ("would_reuse", "unavailable", *REASONS)}
_LOCK = threading.Lock()


def reset_shadow_state() -> None:
    """Tests: drop the cache and zero the counters."""
    with _LOCK:
        _CACHE.clear()
        _FLIGHT.clear()
        _LOOP_MISS_LOGGED.clear()
        for key in list(_COUNTS):
            _COUNTS[key] = 0


def shadow_counts() -> dict[str, int]:
    """The bounded in-process counters (no HTTP endpoint; tests read this)."""
    with _LOCK:
        return dict(_COUNTS)


def _bump(name: str) -> None:
    with _LOCK:
        _COUNTS[name] = _COUNTS.get(name, 0) + 1


def _ready_is_stale(ready: _Ready, now: float) -> bool:
    """True when the summary's generated_at is past SUMMARY_MAX_AGE_H."""
    return now - ready.generated_at_ts > SUMMARY_MAX_AGE_H * 3_600.0


def _record_unavailable(kind: str, location: str, reason: str) -> None:
    """One structured `unavailable` log line + counter bump (never silent).

    The token is deliberately absent here: it must never reach a log.
    """
    _log_event({
        "event": "unavailable",
        "reason": reason,
        "source": kind,
        "location": location,
    })
    _bump("unavailable")


def _fresh_or_unavailable(
    state: _Ready | _Unavailable, key: str, kind: str, location: str, now: float,
    cached_mtime: float | None,
) -> _Ready | _Unavailable:
    """Return a cached state, re-applying the 26 h gate on EVERY return.

    A `_Ready` whose generated_at has grown past SUMMARY_MAX_AGE_H is treated
    as unavailable (logged ONCE per cache period, counted) and cached as such
    until a refresh replaces it; an `_Unavailable` is returned as-is (already
    logged). The cached mtime is reused verbatim: this helper may run on the
    event loop, where an ``os.stat`` would be blocking IO.
    """
    if isinstance(state, _Ready) and _ready_is_stale(state, now):
        unavailable = _Unavailable("summary is stale", logged=True)
        with _LOCK:
            _CACHE[key] = (
                now + SUMMARY_CACHE_S,
                cached_mtime if kind == "file" else None,
                unavailable,
            )
        _record_unavailable(kind, location, "summary is stale")
        return unavailable
    return state


def _loop_miss(key: str, kind: str, location: str, now: float) -> _Unavailable:
    """A cold/expired cache on the event loop: never read or fetch, but say
    so ONCE per cache period (and count it) instead of silently."""
    with _LOCK:
        last = _LOOP_MISS_LOGGED.get(key, -math.inf)
        should_log = now - last >= SUMMARY_CACHE_S
        if should_log:
            _LOOP_MISS_LOGGED[key] = now
    if should_log:
        _record_unavailable(kind, location, "event loop cache miss")
    return _Unavailable("event loop cache miss", logged=True)


def _state(
    kind: str, location: str, token: str, now: float, *, on_loop: bool,
) -> _Ready | _Unavailable | None:
    """The cached summary state, refreshing when the cache period ended.

    None when the feature is off (no source configured). Never reads or
    fetches on the event loop (a cold cache there reads as unavailable, logged
    ONCE per cache period). The 26 h staleness gate is re-applied on every
    cached return, so an expired summary can never come back as a `_Ready`.
    Off the loop, concurrent cold readers single-flight to one fetch.
    """
    if not kind:
        return None
    key = f"{kind}:{location}"
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        expires_at, cached_mtime, state = cached
        # An mtime move only matters off the loop: os.stat is blocking IO and
        # must never run inside the ASGI loop. On the loop the cached mtime is
        # used verbatim; the off-loop capabilities poll re-detects any move.
        mtime_moved = False
        if kind == "file" and cached_mtime is not None and not on_loop:
            mtime_moved = _mtime(location) != cached_mtime
        if not mtime_moved and expires_at > now:
            return _fresh_or_unavailable(state, key, kind, location, now, cached_mtime)
        # Cache period ended (or the file mtime moved): on the loop we may
        # still return a still-fresh `_Ready` (stale is better than blocking)
        # but never a stale one.
        if on_loop and isinstance(state, _Ready):
            return _fresh_or_unavailable(state, key, kind, location, now, cached_mtime)
    if on_loop:
        return _loop_miss(key, kind, location, now)
    # Off loop: single-flight refresh. One thread fetches; the rest wait on
    # the flight event (which is set under _LOCK, never held during a fetch).
    with _LOCK:
        flight = _FLIGHT.get(key)
        if flight is None:
            flight = threading.Event()
            _FLIGHT[key] = flight
            owner = True
        else:
            owner = False
    if not owner:
        flight.wait()
        with _LOCK:
            refreshed = _CACHE.get(key)
        if refreshed is not None:
            return _fresh_or_unavailable(
                refreshed[2], key, kind, location, now, refreshed[1],
            )
        # The owner failed to write (should not happen: fetch errors are
        # converted to _Unavailable). Recurse once to become the owner.
        return _state(kind, location, token, now, on_loop=False)
    state: _Ready | _Unavailable | None = None
    try:
        if kind == "file":
            state = _parse_summary(_read_summary_file(location), now)
        elif not token:
            # A URL without a token never fetches (privacy ruling): the
            # summary is unavailable, logged once per cache period.
            raise SummaryUnavailable("summary token missing")
        else:
            state = _parse_summary(_fetch_summary(location, token), now)
    except SummaryUnavailable as error:
        state = _Unavailable(str(error) or "summary unavailable")
    except Exception as error:  # unexpected: never silent, never cache None
        state = _Unavailable("summary unavailable")
        logger.warning("originality shadow state read failed", exc_info=True)
    finally:
        with _LOCK:
            if state is not None:
                _CACHE[key] = (
                    now + SUMMARY_CACHE_S,
                    _mtime(location) if kind == "file" else None,
                    state,
                )
            flight.set()
            _FLIGHT.pop(key, None)
    if isinstance(state, _Unavailable):
        _record_unavailable(kind, location, state.reason)
        state.logged = True
    return state


def _log_event(payload: dict[str, Any]) -> None:
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _origin_of(master: Any, summary: _Ready) -> tuple[str | None, float | None]:
    """(origin_id, origin start seconds or None) for a planned master."""
    origin = getattr(master, "origin", None)
    if isinstance(origin, dict):
        origin_id = origin.get("originSourceId")
        if isinstance(origin_id, str) and origin_id:
            start = origin.get("originStartMs")
            if isinstance(start, (int, float)) and not isinstance(start, bool) \
                    and math.isfinite(float(start)) and float(start) >= 0:
                return origin_id, float(start) / 1_000.0
            return origin_id, None  # unknown offset: the whole origin is touched
    entry = summary.masters.get(getattr(master, "sha256", ""))
    if isinstance(entry, dict):
        origin_id = entry.get("origin_id")
        if isinstance(origin_id, str) and origin_id:
            offset = entry.get("offset_s")
            if isinstance(offset, (int, float)) and not isinstance(offset, bool) \
                    and math.isfinite(float(offset)):
                if float(offset) >= 0:
                    return origin_id, float(offset)
                # A negative offset is malformed summary data: the cut's
                # origin cannot be named -> unknown_origin, logged.
                return None, None
            return origin_id, None
    return None, None


def _reason(index: _OriginIndex, start: float, end: float, page: str | None,
            now: float) -> str | None:
    """Why [start, end) would reuse shown footage, or None when fresh.

    Reasons are checked in the gate design's priority order (cross_page, then
    shows_cap, then gap_lt_7d) across EVERY overlapping row, so which row
    happens to sort first never decides the logged reason. A call without a
    page id compares nothing: the network rules are per page.
    """
    if page is None:
        return None
    rows = list(index.overlapping(start, end, now))
    if any(row_page != page and age <= CROSS_PAGE_DAYS * _DAY_S
           for (_s, _e, row_page, _shows, _last), age in rows):
        return CROSS_PAGE
    for (_s, _e, row_page, shows, _last), age in rows:
        if row_page == page and shows >= ORIGIN_SECOND_SHOWS_PER_PAGE \
                and age <= ORIGIN_SECOND_COOLDOWN_DAYS * _DAY_S:
            return SHOWS_CAP
    for (_s, _e, row_page, _shows, _last), age in rows:
        if row_page == page and age < ORIGIN_SECOND_MIN_GAP_DAYS * _DAY_S:
            return GAP_LT_7D
    return None


def observe_planned_cuts(
    recipe: Any,
    cuts: Any,
    *,
    page_id: str | None = None,
    job_id: str | None = None,
) -> None:
    """Log every planned cut that would reuse already-shown origin seconds.

    Total best effort: any problem inside is swallowed (the shadow never
    blocks or fails planning), and an unavailable summary logs one
    `unavailable` line per cache period instead of per cut. Only a real job
    (a ``job_id``) logs or counts `would_reuse`: the capabilities poll and the
    empty-plan explainer still keep the cache warm but must not inflate the
    counters or the log volume.
    """
    try:
        kind, location, token = _summary_source()
        if not kind or not cuts:
            return
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        now = _now()
        state = _state(kind, location, token, now, on_loop=on_loop)
        if state is None:
            return
        if isinstance(state, _Unavailable):
            return  # already logged once for this cache period
        if job_id is None:
            return  # warm the cache only; no would_reuse logs or counts
        for cut in cuts:
            master = getattr(cut, "master", None)
            start_ms = getattr(cut, "start_ms", None)
            duration_ms = getattr(cut, "duration_ms", None)
            if master is None or not isinstance(start_ms, (int, float)) \
                    or not isinstance(duration_ms, (int, float)):
                continue
            origin_id, origin_start = _origin_of(master, state)
            if origin_id is None:
                if page_id is None:
                    # Without a page the network rules cannot name an origin;
                    # unknown_origin must never be logged here.
                    continue
                reason = UNKNOWN_ORIGIN
                window = None
                index = None
            else:
                index = state.origins.get(origin_id)
                if index is None:
                    continue  # nothing shown on this origin yet: fresh
                if origin_start is None:
                    # Unknown offset: the whole origin is touched. An origin
                    # without a duration is unbounded; JSON logs must use null
                    # for an open end, never Infinity.
                    start = 0.0
                    end = index.duration_s
                    open_end = not math.isfinite(end)
                    window = [start, None if open_end else end]
                    reason = _reason(
                        index, start, end if not open_end else math.inf, page_id, now,
                    )
                else:
                    start = origin_start + float(start_ms) / 1_000.0
                    end = origin_start + (float(start_ms) + float(duration_ms)) / 1_000.0
                    window = [start, end]
                    reason = _reason(index, start, end, page_id, now)
            if reason is None:
                continue
            _log_event({
                "event": "would_reuse",
                "page": page_id,
                "origin_id": origin_id,
                "window": window,
                "reason": reason,
                "master_sha": getattr(master, "sha256", None),
                "job_id": job_id,
                "summary_generated_at": state.generated_at,
            })
            _bump("would_reuse")
            _bump(reason)
    except Exception:  # the shadow must never break planning
        logger.warning("originality shadow failed", exc_info=True)
        _bump("unavailable")
