"""Originality show-limit enforcement for the Scrambler, from the REAL ledger.

Rule #1 (originality) is immutable: transforms never make reused footage new.
When the Scrambler is ON for a page, its planner refuses any candidate source
window whose ORIGIN seconds the real usage ledger (usage.jsonl - the exact
producer row shape, never a parallel schema) shows were already used:

- at most 2 shows per origin-second per page per 30 days (a SECOND show is
  legal once it is >= 7 days after the first; the third is refused),
- zero shows by any OTHER page within 90 days.

A prior row that NAMES its origin but lost the window (null offsets) is never
fresh evidence: it is stored as a WILDCARD show for that origin identity,
conservatively counting for ANY candidate window of that origin - other page
blocks 90 days, own page counts toward the 30-day cap and the 7-day gap.
Refusing the whole feed over one such row would brick unrelated fresh
sources, so the wildcard stays scoped to its own origin. A row whose
``posted_at`` lies up to 24 h in the future (accepted clock skew) clamps its
age to 0: an own-page future show still blocks the 7-day gap and the 2-show
cap instead of being skipped.

Shows are keyed by the CANONICAL ORIGIN IDENTITY ``(origin_kind, origin_id,
origin_second)`` - never by master SHA alone: the same underlying video
re-encoded under a second master SHA must not slip a 90-day exclusivity. Real
origin ids are case-sensitive Drive/YouTube ids and are never lowercased.
The candidate master's canonical origin comes from the real ``origins.json``
producer map (master SHA -> page, origin_kind, origin_id, origin_start_s/
end_s). A candidate whose master is NOT in the map, or whose mapping carries
null offsets (the upstream window is explicitly lost), FAILS CLOSED: unknown
origin or offset refuses ON rather than assuming fresh footage.
``OriginalityIndex.canonical_origin_window`` is the PRIVATE canonical
accounting for that decision: it maps a master-LOCAL library window (integer
milliseconds, exact bounds) to ABSOLUTE canonical origin milliseconds using
the mapping's ``origin_start_s`` as the upstream offset, validating that the
positive local window lies wholly within the mapped extent. It never mutates
a library descriptor, hash, or the index - no footprint transform escapes
the accounting by moving the window.

Page identity: the real ledger records pages as TikTok handles
(``@miles.of.memories77``) while the Lab addresses pages as ``acct:<uuid>`` /
``tt-*`` ids, so the caller maps its page through the recipe's Master Pages
``handle``. A same-page show is one whose handle matches (``@``-stripped,
case-insensitive) or whose raw page string equals the Lab page id.

Retired footage (``retired-origins.json``, the control-plane list of
fingerprint-verified retired master SHAs) is refused outright. Canonical
origin aliases are derived by joining those SHAs to ``origins.json``:
a retired SHA, and any other master sharing a KNOWN canonical identity
of that retired SHA, never enters a Scrambler window. The retirement file's
family labels (for example ``footage:IMG_4962``) are not the producer's
canonical Drive/YouTube identity namespace. A retired SHA absent from the
origins map supplies no canonical alias; its direct SHA still refuses.

The feeds are REQUIRED for ON: missing, corrupt or absurd inputs raise
``ScramblerOriginalityUnavailable`` and the job fails closed. Freshness by
mtime is enforced for usage.jsonl only (26 h by default). Origins is a
master-identity map and retirement is versioned policy; neither has a file-age
gate. The caller must supply the current authoritative map and retirement
policy; this reader does not certify their publication currency from mtime. The OFF path never reads any of these files. Bounds are enforced
BEFORE any per-second expansion: ledger bytes/rows, window length (<= 1 h),
non-negative finite seconds, no future ``posted_at`` beyond 24 h clock skew,
and an AGGREGATE entry budget (``USAGE_MAX_INDEX_ENTRIES``) that counts every
stored show-entry - duplicates on the same second included - before it is
allocated. Usage is checked with stat, and every feed (usage, origins,
retired) is read with
a byte bound, so a file that grows between stat and read cannot bypass the
cap. Rows older than 90 days (the longest lookback) are never indexed.

Posted-asset release evidence (PRIVATE): the planner may hold a private
allocation hold over a queued/rendered-unposted Lab asset. The ONLY key
that releases such a hold is an OBSERVED POSTED usage row - the real
ledger's ``asset_sha`` naming that exact asset - never a Lab render, a
source-window overlap, or another asset's row. During load, every
validated row inside the 90-day lookback that carries an ``asset_sha``
keeps one COMPACT posting record (verbatim producer ``posted_at`` string,
page, covered whole origin seconds - the same floor/ceil accounting the
limits use) keyed by ``(asset_sha, origin_kind, origin_id)``: no
per-second expansion, and each record is counted in the SAME aggregate
entry budget BEFORE it is stored. Rows older than 90 days are skipped
before capture, so posting proof for them is UNAVAILABLE from this module
by design - the caller persists a copied actual observation when it sees
one; that copied row is real evidence, never a faked ``posted_at``.
``OriginalityIndex.posted_asset_observation`` answers the release query
and returns None conservatively on any mismatch. Its result is private
metadata: it must NEVER be written into hashes, wire formats, or library
fields. ``window_allowed`` remains the authority for posted show limits -
observed evidence releases the private hold, it never doubles as show
permission.

This module is evidence only: it answers "may this page show this origin
window again". The planner's cross-process reservations stay the allocation
authority for races between two jobs.
"""
from __future__ import annotations

import io
import hashlib
import json
from decimal import Decimal
import logging
import math
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ORIGINALITY_USAGE_PATH_ENV = "CONTENT_LAB_ORIGINALITY_USAGE_PATH"
ORIGINALITY_ORIGINS_PATH_ENV = "CONTENT_LAB_ORIGINALITY_ORIGINS_PATH"
ORIGINALITY_RETIRED_PATH_ENV = "CONTENT_LAB_ORIGINALITY_RETIRED_PATH"
ORIGINS_FILE_NAME = "origins.json"
RETIRED_REPO_PATH = Path(__file__).resolve().parent.parent / "recipes" / "originality" / "retired-origins.json"

USAGE_STALE_AFTER_S = 26 * 3600.0
USAGE_MAX_BYTES = 64 * 1024 * 1024
USAGE_MAX_ROWS = 2_000_000
# AGGREGATE expansion bound for the whole ledger, in stored show-entries:
# every per-second append counts, including duplicate shows stored on the
# same second. The budget is checked BEFORE any entry is allocated, so a
# ledger full of long windows (rows x up to 3600 s) cannot expand into
# unbounded per-second entries.
USAGE_MAX_INDEX_ENTRIES = 500_000
# The control-plane maps are small JSON documents, but their reads are byte
# bounded too: no feed read in this module is unbounded.
ORIGINS_MAX_BYTES = 8 * 1024 * 1024
RETIRED_MAX_BYTES = 1024 * 1024
# A show window longer than an hour is producer corruption, not a clip.
SHOW_WINDOW_MAX_S = 3600.0
# A posted_at further in the future than a day of clock skew is corrupt.
POSTED_FUTURE_SKEW_S = 24 * 3600.0

SAME_PAGE_WINDOW_S = 30 * 86_400.0
SAME_PAGE_MAX_SHOWS = 2
SAME_PAGE_GAP_S = 7 * 86_400.0
CROSS_PAGE_WINDOW_S = 90 * 86_400.0

_logged_once: set[str] = set()


def _log_once(key: str, message: str, *args, level: int = logging.WARNING) -> None:
    if key in _logged_once:
        return
    _logged_once.add(key)
    log.log(level, "content_lab.scrambler-originality.%s " + message, key, *args)


class ScramblerOriginalityUnavailable(RuntimeError):
    """The authoritative originality feed is missing or unusable: refuse ON.

    Named reasons: ``usage_path_unconfigured``, ``usage_path_missing``,
    ``usage_ledger_stale``, ``usage_ledger_too_large``, ``usage_line_corrupt``,
    ``usage_row_invalid``, ``origins_map_invalid``, ``retired_list_invalid``.
    Never crossed with a fallback: the job fails closed instead of planning
    against unknown shows.
    """


def _parse_epoch(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _normalize_master_sha(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        return None
    return text


def normalize_handle(value) -> str:
    """Handle comparison key: TikTok handles are @-prefixed and case-insensitive.

    ORIGIN ids are never touched here - only page handles fold case.
    """
    return (value or "").strip().lstrip("@").casefold()


def _finite_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _integral_ms(value) -> int | None:
    """Exact integer milliseconds, or None: callers must pass whole ms."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number != int(number):
        return None
    return int(number)


@dataclass(frozen=True)
class _Show:
    page: str
    posted_epoch: float


@dataclass(frozen=True)
class _OriginWindow:
    """A master's canonical origin identity + upstream window (or None)."""

    origin_kind: str
    origin_id: str
    # (start_s, end_s) in origin seconds; None when the producer explicitly
    # recorded the upstream window as lost - such candidates fail closed.
    window: tuple[float, float] | None


@dataclass(frozen=True)
class _PostedObservation:
    """One REAL posted usage row's compact release evidence (PRIVATE).

    ``first_second``/``last_second`` are the row's covered whole origin
    seconds under the SAME floor/ceil accounting ``window_allowed``
    enforces; both None means a known-origin WILDCARD row - the producer
    lost the window, so it conservatively counts for EVERY second of that
    origin. ``posted_at`` is the VERBATIM producer string, never a
    re-formatted or invented timestamp.
    """

    page: str
    posted_at: str
    posted_epoch: float
    first_second: int | None
    last_second: int | None


@dataclass(frozen=True)
class OriginalityIndex:
    """Canonical-origin show history, built once per job creation.

    ``shows`` keys are ``(origin_kind, origin_id, origin_second)`` - origin
    ids keep their exact case. ``wildcard_shows`` keys are
    ``(origin_kind, origin_id)``: prior usage that named the origin but lost
    the window, counting for ANY candidate window of that origin.
    ``origins`` maps master SHA -> canonical origin window.
    ``retired_masters``/``retired_origins`` carry the canonical retirement
    list and its origin aliases. ``posted_assets`` is the PRIVATE
    posted-asset evidence map: ``(asset_sha, origin_kind, origin_id)`` ->
    compact per-row posting records (no per-second expansion) used ONLY
    by ``posted_asset_observation``.
    """

    shows: dict[tuple[str, str, int], tuple[_Show, ...]] = field(default_factory=dict)
    wildcard_shows: dict[tuple[str, str], tuple[_Show, ...]] = field(default_factory=dict)
    origins: dict[str, _OriginWindow] = field(default_factory=dict)
    retired_masters: frozenset[str] = frozenset()
    retired_origins: frozenset[tuple[str, str]] = frozenset()
    posted_assets: dict[tuple[str, str, str], tuple[_PostedObservation, ...]] = field(
        default_factory=dict
    )

    def canonical_origin_window(
        self, master_sha: str, library_start_ms: int, duration_ms: int,
    ) -> tuple[str, str, int, int]:
        """Map a master-LOCAL library window to ABSOLUTE canonical origin ms.

        Returns ``(origin_kind, origin_id, absoluteOriginStartMs,
        absoluteOriginEndMs)``: the mapping's ``origin_start_s`` is the
        upstream offset, so ``absoluteOriginStartMs = origin_start_s * 1000 +
        library_start_ms`` and ``absoluteOriginEndMs`` adds ``duration_ms``
        (end exclusive). The caller MUST pass integer milliseconds and exact
        bounds - the local window must be positive (start >= 0, duration > 0)
        and lie wholly within the mapped extent. An unknown master mapping or
        a mapping whose upstream window is lost raises
        ``ScramblerOriginalityUnavailable``.

        This is PRIVATE canonical accounting for the Scrambler planner and
        executor: a pure query that NEVER changes a library descriptor, a
        hash, or the index. The returned pair feeds ``window_allowed``
        verbatim.
        """
        master = _normalize_master_sha(master_sha) or (master_sha or "").lower()
        mapping = self.origins.get(master)
        if mapping is None:
            raise ScramblerOriginalityUnavailable(
                f"origin_mapping_unknown:{master[:12]}"
            )
        if mapping.window is None:
            raise ScramblerOriginalityUnavailable(
                f"origin_window_unknown:{mapping.origin_kind}:{mapping.origin_id}"
            )
        start_ms = _integral_ms(library_start_ms)
        duration = _integral_ms(duration_ms)
        if start_ms is None or duration is None or start_ms < 0 or duration <= 0:
            raise ScramblerOriginalityUnavailable(
                f"candidate_window_invalid: local window "
                f"[{library_start_ms}, +{duration_ms}] ms is not a positive "
                f"integer-millisecond window"
            )
        offset_ms = Decimal(str(mapping.window[0])) * 1000
        if offset_ms != int(offset_ms):
            raise ScramblerOriginalityUnavailable(
                f"origins_map_invalid: {master[:12]} carries a fractional "
                f"millisecond offset {mapping.window[0]}"
            )
        origin_start = int(offset_ms) + start_ms
        origin_end = origin_start + duration
        if origin_end > Decimal(str(mapping.window[1])) * 1000:
            raise ScramblerOriginalityUnavailable(
                f"candidate_window_invalid: local window "
                f"[{library_start_ms}, +{duration_ms}] ms leaves the mapped "
                f"extent {mapping.window[0]}..{mapping.window[1]} s"
            )
        return mapping.origin_kind, mapping.origin_id, origin_start, origin_end

    def window_allowed(
        self, page_id: str, master_sha: str,
        origin_start_ms: int, origin_end_ms: int, *,
        page_handles: frozenset[str] = frozenset(),
        now_epoch: float | None = None,
    ) -> tuple[bool, str]:
        """(allowed, reason) for one page showing one origin window again.

        ``origin_start_ms``/``origin_end_ms`` are ABSOLUTE canonical origin
        milliseconds - the exact integer pair returned by
        ``canonical_origin_window``. An invalid window (non-integer
        milliseconds, negative start, end <= start) or one not lying wholly
        inside the master's mapped extent is refused. Retired masters and
        retired origin aliases refuse outright. Wildcard prior usage (known
        origin, lost window) counts for EVERY candidate second of that
        origin. Every whole origin second the window covers must then satisfy
        all three show limits; the first violated second names the refusal.
        A same-page show up to 24 h in the future clamps its age to 0, so it
        still blocks the 7-day gap and counts toward the 2-show cap.
        """
        master = _normalize_master_sha(master_sha) or (master_sha or "").lower()
        now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
        if _finite_number(now) is None:
            return False, "clock_invalid"
        if master in self.retired_masters:
            return False, f"retired_master:{master[:12]}"
        mapping = self.origins.get(master)
        if mapping is None:
            return False, f"origin_mapping_unknown:{master[:12]}"
        if mapping.window is None:
            return False, f"origin_window_unknown:{mapping.origin_kind}:{mapping.origin_id}"
        if (mapping.origin_kind, mapping.origin_id) in self.retired_origins:
            return False, f"retired_origin:{mapping.origin_kind}:{mapping.origin_id}"
        start_ms = _integral_ms(origin_start_ms)
        end_ms = _integral_ms(origin_end_ms)
        if start_ms is None or end_ms is None or start_ms < 0 or end_ms <= start_ms:
            return False, f"candidate_window_invalid:[{origin_start_ms}, {origin_end_ms}] ms"
        if start_ms < Decimal(str(mapping.window[0])) * 1000 or end_ms > Decimal(str(mapping.window[1])) * 1000:
            return False, (
                f"candidate_window_outside_master:[{start_ms}, {end_ms}] ms "
                f"not within {mapping.window[0] * 1000:.0f}..{mapping.window[1] * 1000:.0f} ms"
            )
        mine = frozenset(normalize_handle(h) for h in page_handles)
        # Wildcard prior usage (known origin, lost window) blocks ANY
        # candidate window of this origin - it is never fresh evidence.
        wildcard_own_recent = 0
        for show in self.wildcard_shows.get((mapping.origin_kind, mapping.origin_id), ()):
            own = show.page == page_id or normalize_handle(show.page) in mine
            age = now - show.posted_epoch
            if not own:
                if age < CROSS_PAGE_WINDOW_S:
                    return False, "cross_page_90d:origin_window_unknown"
                continue
            own_age = max(age, 0.0)
            if own_age < SAME_PAGE_WINDOW_S:
                if own_age < SAME_PAGE_GAP_S:
                    return False, "page_second_gap_7d:origin_window_unknown"
                wildcard_own_recent += 1
        first = int(start_ms // 1_000)
        last = int(max(start_ms, end_ms - 1) // 1_000)
        for second in range(first, last + 1):
            same_page_recent = wildcard_own_recent
            for show in self.shows.get((mapping.origin_kind, mapping.origin_id, second), ()):
                own = show.page == page_id or normalize_handle(show.page) in mine
                age = now - show.posted_epoch
                if not own:
                    if age < CROSS_PAGE_WINDOW_S:
                        return False, f"cross_page_90d:second={second}"
                    continue
                own_age = max(age, 0.0)
                if own_age < SAME_PAGE_WINDOW_S:
                    if own_age < SAME_PAGE_GAP_S:
                        return False, f"page_second_gap_7d:second={second}"
                    same_page_recent += 1
            if same_page_recent >= SAME_PAGE_MAX_SHOWS:
                return False, f"page_second_cap_2_30d:second={second}"
        return True, ""

    def posted_asset_observation(
        self, asset_sha: str, origin_kind: str, origin_id: str,
        origin_start_ms: int, origin_end_ms: int,
    ) -> dict | None:
        """Observed-posted evidence for ONE exact Lab asset, or None.

        PRIVATE release query for the planner's asset holds. It returns a
        FRESH small dict ``{"page", "posted_at", "posted_epoch"}`` - the
        VERBATIM producer ``posted_at`` string, its parsed epoch and the
        producer page of a REAL validated posted usage row - only when a
        row carries that exact ``asset_sha`` (hex prefix/case normalized;
        origin ids are NEVER normalized and match case-sensitively), names
        that exact origin identity, and covers EVERY whole origin second
        of the requested canonical window ``[origin_start_ms,
        origin_end_ms)`` (positive integer milliseconds; the same
        floor/ceil second accounting ``window_allowed`` enforces). A
        matching known-origin WILDCARD row (lost window) covers every
        second, exactly as it conservatively blocks for every second.
        When several rows qualify, the LATEST posting wins. Anything else
        - unknown asset, identity or case mismatch, partial coverage,
        invalid bounds - returns None CONSERVATIVELY: a hold is never
        released on another asset's row, a partial range, an invented
        timestamp, a Lab render, or source-window overlap alone. Rows
        older than the 90-day lookback left no proof here (documented
        unavailability). This is evidence only: ``window_allowed`` still
        governs the posted show limits without double counting the hold,
        and the returned dict is private metadata that must NEVER be
        written into hashes, wire formats, or library fields; mutating it
        cannot affect the index.
        """
        asset = _normalize_master_sha(asset_sha)
        if asset is None:
            return None
        if not isinstance(origin_kind, str) or not origin_kind \
                or not isinstance(origin_id, str) or not origin_id:
            return None
        start_ms = _integral_ms(origin_start_ms)
        end_ms = _integral_ms(origin_end_ms)
        if start_ms is None or end_ms is None or start_ms < 0 or end_ms <= start_ms:
            return None
        first = int(start_ms // 1_000)
        last = int((end_ms - 1) // 1_000)
        best: _PostedObservation | None = None
        for record in self.posted_assets.get((asset, origin_kind, origin_id), ()):
            if record.first_second is None:
                covered = True  # known-origin wildcard: every second counts
            else:
                covered = record.first_second <= first and record.last_second >= last
            if covered and (best is None or record.posted_epoch >= best.posted_epoch):
                best = record
        if best is None:
            return None
        return {"page": best.page, "posted_at": best.posted_at,
                "posted_epoch": best.posted_epoch}

    def seconds_indexed(self) -> int:
        return len(self.shows)


def _parse_usage_line(line: str, *, number: int) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError as error:
        raise ScramblerOriginalityUnavailable(
            f"usage_line_corrupt: line {number}: {error}"
        ) from error
    if row is None:
        return None
    if not isinstance(row, dict):
        raise ScramblerOriginalityUnavailable(f"usage_row_invalid: line {number}")
    return row


def _row_show(row: dict, *, number: int, origins: dict[str, _OriginWindow],
              now_epoch: float) -> tuple[str, str, int | None, int | None, _Show] | None:
    """One ledger row -> (origin_kind, origin_id, first_second, last_second,
    show), or None when the row is irrelevant to source-window limits.

    Attribution order: the row's OWN origin identity when the producer
    recovered it; else the canonical map for the row's master SHA. Rows with
    neither identity (generated content) are inert for per-second limits -
    they cannot collide with a keyed candidate. A row that NAMES an origin
    but carries an impossible window or timestamp fails closed: half-known
    evidence never under-counts silently.

    A row older than 90 days (the longest lookback of any limit) is skipped
    un-indexed: it can never block a candidate, so millions of stale
    entries are not allocated for it. A row that NAMES an origin but lost
    the window entirely (both offsets null) comes back with null seconds:
    the caller stores it as a WILDCARD show for that origin identity -
    known-origin prior usage with an unknown window is never fresh.
    """
    page = row.get("page")
    if (not isinstance(page, str) or not page.strip() or len(page) > 256
            or not page.isprintable()):
        raise ScramblerOriginalityUnavailable(f"usage_row_invalid: line {number}: page")
    posted = _parse_epoch(row.get("posted_at"))
    if posted is None:
        raise ScramblerOriginalityUnavailable(
            f"usage_row_invalid: line {number}: posted_at"
        )
    if posted - now_epoch > POSTED_FUTURE_SKEW_S:
        raise ScramblerOriginalityUnavailable(
            f"usage_row_invalid: line {number}: posted_at in the future"
        )
    if now_epoch - posted >= CROSS_PAGE_WINDOW_S:
        return None  # older than every lookback: irrelevant, never indexed
    kind = row.get("origin_kind")
    origin_id = row.get("origin_id")
    master = _normalize_master_sha(row.get("master_sha"))
    if not isinstance(kind, str) or not kind or not isinstance(origin_id, str) or not origin_id:
        kind = origin_id = None
    if kind is None and master is not None:
        mapping = origins.get(master)
        if mapping is not None:
            kind, origin_id = mapping.origin_kind, mapping.origin_id
    if kind is None:
        return None
    start_s = _finite_number(row.get("origin_start_s"))
    end_s = _finite_number(row.get("origin_end_s"))
    if start_s is None and end_s is None:
        # The producer recorded the origin id but lost the window: this row
        # cannot place a show in time, so it counts for EVERY window of that
        # origin as a wildcard instead of being treated as fresh.
        return kind, origin_id, None, None, _Show(page=page, posted_epoch=posted)
    if start_s is None or end_s is None or end_s <= start_s:
        raise ScramblerOriginalityUnavailable(
            f"usage_row_invalid: line {number}: origin window half-known or nonpositive"
        )
    # Bounds BEFORE range expansion: negative starts, absurd spans or
    # non-finite values refuse the whole feed instead of allocating wild
    # millions of per-second entries.
    if start_s < 0 or end_s - start_s > SHOW_WINDOW_MAX_S:
        raise ScramblerOriginalityUnavailable(
            f"usage_row_invalid: line {number}: origin window out of bounds "
            f"[{start_s}, {end_s}]"
        )
    first = int(math.floor(start_s))
    last = int(math.ceil(end_s)) - 1
    return kind, origin_id, first, last, _Show(page=page, posted_epoch=posted)


def _read_bounded(path: Path, max_bytes: int, reason: str) -> bytes:
    """Read at most ``max_bytes`` bytes: the stat-time size check cannot be
    trusted alone, because the file may grow between stat and read."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as error:
        raise ScramblerOriginalityUnavailable(
            f"{reason}: cannot read {path}: {error}"
        ) from error
    if len(raw) > max_bytes:
        raise ScramblerOriginalityUnavailable(
            f"{reason}: {path} is more than {max_bytes} bytes"
        )
    return raw


def _load_origins_map(origins_path: Path, raw: bytes | None = None) -> dict[str, _OriginWindow]:
    """The real producer map: master SHA -> canonical origin + window.

    A missing/unparseable map refuses ON (candidates could not be keyed).
    Entries with a null origin id mean the master's origin was never
    recovered: they map to nothing, so any candidate on that master fails
    closed as ``origin_mapping_unknown``. Null offsets mean the upstream
    window is explicitly lost: the identity maps, the window does not.
    """
    if raw is None:
        raw = _read_bounded(origins_path, ORIGINS_MAX_BYTES, "origins_map_invalid")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        raise ScramblerOriginalityUnavailable(
            f"origins_map_invalid: {origins_path} is not valid JSON: {error}"
        ) from error
    if not isinstance(data, dict):
        raise ScramblerOriginalityUnavailable(f"origins_map_invalid: {origins_path}")
    origins: dict[str, _OriginWindow] = {}
    seen_masters: set[str] = set()
    for sha, entry in data.items():
        master = _normalize_master_sha(sha)
        if master is None or not isinstance(entry, dict):
            raise ScramblerOriginalityUnavailable(f"origins_map_invalid: {origins_path}")
        if master in seen_masters:
            raise ScramblerOriginalityUnavailable(
                f"origins_map_invalid: duplicated normalized master {master[:12]}")
        seen_masters.add(master)
        kind = entry.get("origin_kind")
        origin_id = entry.get("origin_id")
        if not isinstance(kind, str) or not kind or not isinstance(origin_id, str) \
                or not origin_id:
            continue  # unknown origin: absent from the map -> candidate refuses
        start_s = _finite_number(entry.get("origin_start_s"))
        end_s = _finite_number(entry.get("origin_end_s"))
        if start_s is None or end_s is None or end_s < start_s or start_s < 0 \
                or end_s - start_s > SHOW_WINDOW_MAX_S * 24:
            window = None  # lost upstream window: identity known, window not
        else:
            window = (start_s, end_s)
        origins[master] = _OriginWindow(origin_kind=kind, origin_id=origin_id,
                                        window=window)
    return origins


def _load_retired(retired_path: Path) -> frozenset[str]:
    """The canonical retired list of real retired master SHAs.

    Origin aliases are derived in the loader from the origins map: when a
    retired master maps to a canonical origin, that ORIGIN IDENTITY is retired
    too - a re-encoded copy of the same video under a new SHA refuses through
    the alias.
    """
    raw = _read_bounded(retired_path, RETIRED_MAX_BYTES, "retired_list_invalid")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        raise ScramblerOriginalityUnavailable(
            f"retired_list_invalid: {retired_path} is not valid JSON: {error}"
        ) from error
    if not isinstance(data, list):
        raise ScramblerOriginalityUnavailable(f"retired_list_invalid: {retired_path}")
    shas: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("master_sha256s"), list):
            raise ScramblerOriginalityUnavailable(f"retired_list_invalid: {retired_path}")
        for sha in entry["master_sha256s"]:
            master = _normalize_master_sha(sha)
            if master is None or len(master) != 64:
                raise ScramblerOriginalityUnavailable(
                    f"retired_list_invalid: {retired_path}: bad master sha"
                )
            shas.add(master)
    return frozenset(shas)


def load_originality_index(
    path: str | Path | None = None, *,
    origins_path: str | Path | None = None,
    retired_path: str | Path | None = None,
    now_epoch: float | None = None,
    stale_after_s: float = USAGE_STALE_AFTER_S,
    _snapshot: tuple[bytes, bytes, float] | None = None,
) -> OriginalityIndex:
    """Build the canonical-origin show index from the real feed.

    The Scrambler is ON when this runs; the feed is REQUIRED: an unset usage
    path raises ``usage_path_unconfigured`` (the Lab's own reservations are
    allocation authority for races, but they are NOT evidence about history -
    unknown cross-page usage refuses ON). Missing, stale, oversized or corrupt
    data raises with the named reason. The OFF path never calls this.
    """
    raw = path if path is not None else os.environ.get(ORIGINALITY_USAGE_PATH_ENV)
    if raw is None or not str(raw).strip():
        raise ScramblerOriginalityUnavailable(
            "usage_path_unconfigured: the Scrambler requires the authoritative "
            f"usage ledger ({ORIGINALITY_USAGE_PATH_ENV})"
        )
    file_path = Path(str(raw).strip())
    if not file_path.is_file():
        raise ScramblerOriginalityUnavailable(f"usage_path_missing: {file_path}")
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    if _finite_number(now) is None:
        raise ScramblerOriginalityUnavailable("usage_clock_invalid")
    try:
        stat = file_path.stat()
    except OSError as error:
        raise ScramblerOriginalityUnavailable(
            f"usage_ledger_stale: cannot stat {file_path}: {error}"
        ) from error
    if now - stat.st_mtime > stale_after_s:
        raise ScramblerOriginalityUnavailable(
            f"usage_ledger_stale: {file_path} is {int((now - stat.st_mtime) / 3600)} h old"
        )
    if stat.st_size > USAGE_MAX_BYTES:
        raise ScramblerOriginalityUnavailable(
            f"usage_ledger_too_large: {file_path} is {stat.st_size} bytes"
        )
    resolved_origins = (
        Path(origins_path) if origins_path is not None else
        Path(os.environ[ORIGINALITY_ORIGINS_PATH_ENV])
        if os.environ.get(ORIGINALITY_ORIGINS_PATH_ENV) else
        file_path.parent / ORIGINS_FILE_NAME
    )
    resolved_retired = (
        Path(retired_path) if retired_path is not None else
        Path(os.environ[ORIGINALITY_RETIRED_PATH_ENV])
        if os.environ.get(ORIGINALITY_RETIRED_PATH_ENV) else
        RETIRED_REPO_PATH
    )
    if _snapshot is None:
        _snapshot = _verified_feed_snapshot(file_path, resolved_origins)
    usage_raw, origins_raw, usage_mtime = _snapshot
    if now - usage_mtime > stale_after_s:
        raise ScramblerOriginalityUnavailable("usage_ledger_stale: verified feed is too old")
    origins = _load_origins_map(resolved_origins, origins_raw)
    retired_masters = _load_retired(resolved_retired)
    # Origin aliases of retired masters: the retired IDENTITY, not just SHAs.
    retired_origin_ids = frozenset(
        (mapping.origin_kind, mapping.origin_id)
        for sha in retired_masters
        if (mapping := origins.get(sha)) is not None
    )
    # Bounded read: the stat check above is only a fast precheck, the read
    # itself is capped so a ledger growing between stat and read cannot
    # deliver unbounded bytes here.
    try:
        text = usage_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ScramblerOriginalityUnavailable(
            f"usage_line_corrupt: cannot decode {file_path}: {error}"
        ) from error
    shows: dict[tuple[str, str, int], list[_Show]] = {}
    wildcards: dict[tuple[str, str], list[_Show]] = {}
    posted_assets: dict[tuple[str, str, str], list[_PostedObservation]] = {}
    entries = 0
    rows = 0
    # io.StringIO streams the bounded buffer line by line: no second full
    # copy of every ledger line is materialized.
    for number, line in enumerate(io.StringIO(text), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        rows += 1
        if rows > USAGE_MAX_ROWS:
            raise ScramblerOriginalityUnavailable(
                f"usage_ledger_too_large: {file_path} exceeds {USAGE_MAX_ROWS} rows"
            )
        row = _parse_usage_line(stripped, number=number)
        if row is None:
            continue
        parsed = _row_show(row, number=number, origins=origins, now_epoch=now)
        if parsed is None:
            continue
        kind, origin_id, first, last, show = parsed
        asset = _normalize_master_sha(row.get("asset_sha"))
        if asset is not None:
            # PRIVATE posted-asset release evidence: one COMPACT record per
            # validated row (verbatim producer posted_at, page, covered
            # seconds - no per-second expansion), counted in the SAME
            # aggregate entry budget BEFORE it is stored. A garbage or
            # absent asset_sha is simply not evidence: it can never match a
            # query, so it never releases a hold and never bricks the feed.
            entries += 1
            if entries > USAGE_MAX_INDEX_ENTRIES:
                raise ScramblerOriginalityUnavailable(
                    f"usage_ledger_too_large: index would store more than "
                    f"{USAGE_MAX_INDEX_ENTRIES} show-entries"
                )
            posted_assets.setdefault((asset, kind, origin_id), []).append(
                _PostedObservation(
                    page=show.page, posted_at=row["posted_at"],
                    posted_epoch=show.posted_epoch,
                    first_second=first, last_second=last)
            )
        if first is None or last is None:
            # Wildcard show: one stored entry, no per-second expansion.
            entries += 1
            if entries > USAGE_MAX_INDEX_ENTRIES:
                raise ScramblerOriginalityUnavailable(
                    f"usage_ledger_too_large: index would store more than "
                    f"{USAGE_MAX_INDEX_ENTRIES} show-entries"
                )
            wildcards.setdefault((kind, origin_id), []).append(show)
            continue
        span = last - first + 1
        entries += span
        if entries > USAGE_MAX_INDEX_ENTRIES:
            # Aggregate bound BEFORE allocating: every stored show-entry
            # counts, including duplicates landing on the same second.
            raise ScramblerOriginalityUnavailable(
                f"usage_ledger_too_large: index would store more than "
                f"{USAGE_MAX_INDEX_ENTRIES} show-entries"
            )
        for second in range(first, last + 1):
            shows.setdefault((kind, origin_id, second), []).append(show)
    frozen = {key: tuple(value) for key, value in shows.items()}
    frozen_wildcards = {key: tuple(value) for key, value in wildcards.items()}
    frozen_posted = {key: tuple(value) for key, value in posted_assets.items()}
    _log_once(
        "usage-loaded",
        "loaded %d indexed canonical-origin seconds (%d wildcard origins) "
        "from %s (origins %s, retired %s)",
        len(frozen), len(frozen_wildcards), file_path,
        resolved_origins, resolved_retired,
        level=logging.INFO,
    )
    return OriginalityIndex(
        shows=frozen, wildcard_shows=frozen_wildcards, origins=origins,
        retired_masters=retired_masters, retired_origins=retired_origin_ids,
        posted_assets=frozen_posted,
    )


# One parsed index is reused while the feed files are unchanged (path, inode,
# size and mtime of usage, origins and retired) and for at most this long, so
# an enqueue and every cut of a job share one parse instead of re-reading up
# to 64 MiB each time. Freshness is still checked on every call.
INDEX_CACHE_MAX_AGE_S = 600.0
_index_cache_lock = threading.Lock()
_index_cache: list = []  # [(key, built_at_epoch, index)]


def _feed_stat_key(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ctime_ns)


def _strict_feed_identity(path: Path):
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ScramblerOriginalityUnavailable(f"feed_snapshot_invalid: cannot inspect {path}: {error}") from error
    if not stat.S_ISREG(value.st_mode):
        raise ScramblerOriginalityUnavailable(f"feed_snapshot_invalid: not a regular file: {path}")
    return (str(path), value.st_ino, value.st_size, value.st_mtime_ns, value.st_dev, value.st_ctime_ns)


def _verified_feed_snapshot(usage: Path, origins: Path) -> tuple[bytes, bytes, float]:
    """Read one bounded generation; a managed feed must match its commit manifest."""
    from services import scrambler_feed_pull as feed
    manifest = usage.parent / "manifest.json"
    marker = usage.parent / ".scrambler-feed.lock"
    paths = (manifest, marker, usage, origins)
    before = tuple(_strict_feed_identity(path) for path in paths)
    if before[1] is not None and before[0] is None:
        raise ScramblerOriginalityUnavailable("feed_snapshot_invalid: managed manifest missing")
    if before[0] is not None and origins.parent != usage.parent:
        raise ScramblerOriginalityUnavailable("feed_snapshot_invalid: non-colocated managed feed")
    manifest_raw = (_read_bounded(manifest, 1024 * 1024, "feed_snapshot_invalid")
                    if before[0] is not None else None)
    origin_raw = _read_bounded(origins, ORIGINS_MAX_BYTES, "origins_map_invalid")
    usage_raw = _read_bounded(usage, USAGE_MAX_BYTES, "usage_ledger_too_large")
    if before != tuple(_strict_feed_identity(path) for path in paths):
        raise ScramblerOriginalityUnavailable("feed_snapshot_invalid: feed changed while reading")
    if before[2] is None or before[3] is None:
        raise ScramblerOriginalityUnavailable("feed_snapshot_invalid: feed file missing")
    usage_mtime = before[2][3] / 1e9
    if manifest_raw is not None:
        try:
            parsed = feed.parse_manifest(manifest_raw)
            if parsed.generated_at_epoch is None or not math.isfinite(parsed.generated_at_epoch):
                raise ValueError("invalid generation time")
            usage_mtime = min(usage_mtime, parsed.generated_at_epoch)
            for name, data in (("usage.jsonl", usage_raw), ("origins.json", origin_raw)):
                feed._verify_sha(name, data, parsed)
            if feed.count_usage_rows(usage_raw) != parsed.rows:
                raise ValueError("row count mismatch")
        except (feed.ScramblerFeedPullError, ValueError) as error:
            raise ScramblerOriginalityUnavailable(f"feed_snapshot_invalid: {error}") from error
    return usage_raw, origin_raw, usage_mtime


def load_originality_index_cached(*, now_epoch: float | None = None) -> OriginalityIndex:
    """``load_originality_index()`` from the environment, reusing an unchanged parse.

    Any change to a feed file (or a cache older than INDEX_CACHE_MAX_AGE_S)
    reparses; a usage ledger that has gone stale since the parse refuses
    exactly like a fresh load. Errors are never cached.
    """
    raw = os.environ.get(ORIGINALITY_USAGE_PATH_ENV)
    if raw is None or not str(raw).strip():
        return load_originality_index(now_epoch=now_epoch)
    usage = Path(str(raw).strip())
    origins = (Path(os.environ[ORIGINALITY_ORIGINS_PATH_ENV])
               if os.environ.get(ORIGINALITY_ORIGINS_PATH_ENV) else usage.parent / ORIGINS_FILE_NAME)
    retired = (Path(os.environ[ORIGINALITY_RETIRED_PATH_ENV])
               if os.environ.get(ORIGINALITY_RETIRED_PATH_ENV) else RETIRED_REPO_PATH)
    snapshot = _verified_feed_snapshot(usage, origins)
    key = (hashlib.sha256(snapshot[0]).digest(), hashlib.sha256(snapshot[1]).digest(),
           snapshot[2], _feed_stat_key(retired))
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    if None not in key:
        with _index_cache_lock:
            cached = _index_cache[0] if _index_cache else None
        if (cached is not None and cached[0] == key
                and 0 <= now - cached[1] <= INDEX_CACHE_MAX_AGE_S
                and now - key[2] <= USAGE_STALE_AFTER_S):
            return cached[2]
    index = load_originality_index(now_epoch=now_epoch, _snapshot=snapshot)
    if None not in key:
        with _index_cache_lock:
            _index_cache[:] = [(key, now, index)]
    return index


__all__ = [
    "ORIGINALITY_ORIGINS_PATH_ENV", "ORIGINALITY_RETIRED_PATH_ENV",
    "ORIGINALITY_USAGE_PATH_ENV", "OriginalityIndex",
    "ScramblerOriginalityUnavailable", "load_originality_index",
    "load_originality_index_cached", "normalize_handle",
]
