"""Private last-24-hour Lab RENDERED-cut report; no publication or scheduling.

Includes retained rendered prefixes of failed jobs. These are not posted or
delivered metrics; the existing fleet consumer combines posting facts separately.

Exit 0: no alerts (including no implicitly active pages). Exit 1: repeat/zero
alerts. Exit 2: unavailable history, clock or configuration. Repeated --page
explicitly reports historical pages even when OFF; otherwise SCRAMBLER_PAGES
is filtered through the current core gate. Disabled/denied pages are skipped;
missing Worker compatibility acknowledgment is unavailable, never a false zero.

--preflight checks the Scrambler ON-path configuration instead (every pilot
setting, the originality feed and its freshness, the durable history path and
the nightly feed-pull settings), prints one JSON line naming every problem, and
exits 0 when ready or 1 when anything is missing. It never writes.

--preflight --as-enabled runs the same checks as if SCRAMBLER_ENABLED were 1
(the process environment is not changed) and also requires proof of one
verified feed pull (manifest.json beside the usage ledger, matching sha256s and
rows, generated_at at most 20 h old). It is the gate run BEFORE the pilot flip:
exit 0 ready, 1 not ready. It never writes.

--pull-feed-once runs ONE originality feed pull now, regardless of
SCRAMBLER_ENABLED, through the same verified, atomic write path as the hourly
pull (services/scrambler_feed_pull.py run_feed_pull_once). It prints one JSON
line. On error it names only the error class, the stable reason name, the feed
file and the R2 error Code; never a key, endpoint, path or other env value.
Exit codes: 0 pulled and verified; 3 R2 access denied (the Lab's R2 key cannot
read the feed bucket); 4 manifest.json (or a data file) missing in R2 (the
producer has not run); 5 manifest invalid, sha256/rows mismatch or unreadable
generated_at (old files kept); 6 anything else (no such bucket, network, unset
env, write failure). 1 and 2 are left to Python and argparse.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import scrambler as core

SCHEMA = "content-lab.scrambler-daily-report.v1"
PREFLIGHT_SCHEMA = "content-lab.scrambler-preflight.v1"
PULL_SCHEMA = "content-lab.scrambler-feed-pull-once.v1"

PULL_EXIT_OK = 0
PULL_EXIT_ACCESS_DENIED = 3
PULL_EXIT_FEED_MISSING = 4
PULL_EXIT_FEED_INVALID = 5
PULL_EXIT_OTHER = 6

# R2 (S3 API) error Codes that mean "this key may not read the bucket".
_ACCESS_DENIED_CODES = frozenset({
    "AccessDenied", "AllAccessDisabled", "InvalidAccessKeyId",
    "SignatureDoesNotMatch", "Forbidden", "Unauthorized", "401", "403",
})
_MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
_INVALID_REASONS = frozenset({"manifest_invalid", "sha256_missing",
                              "sha256_mismatch", "rows_mismatch"})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _causes(error: BaseException):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def _r2_error_code(error: BaseException) -> str | None:
    """The botocore ClientError ``response["Error"]["Code"]`` in the cause chain."""
    for cause in _causes(error):
        response = getattr(cause, "response", None)
        if isinstance(response, dict) and isinstance(response.get("Error"), dict):
            code = str(response["Error"].get("Code", ""))
            return code if _SAFE_TOKEN.match(code) else None
    return None


def classify_pull_error(error: BaseException, feed_files) -> tuple[int, dict]:
    """(exit code, JSON fields) for a failed pull; names only, never values."""
    reason = getattr(error, "reason", None)
    parts = str(reason).split(":", 2) if isinstance(reason, str) else []
    reason_name = parts[0] if parts and _SAFE_TOKEN.match(parts[0]) else None
    feed_file = parts[1].strip() if len(parts) > 1 and parts[1].strip() in feed_files else None
    code = _r2_error_code(error)
    root = list(_causes(error))[-1]
    if code in _ACCESS_DENIED_CODES:
        exit_code, error_class = PULL_EXIT_ACCESS_DENIED, "r2_access_denied"
    elif code in _MISSING_CODES:
        exit_code = PULL_EXIT_FEED_MISSING
        error_class = "manifest_missing" if feed_file in (None, "manifest.json") else "feed_file_missing"
    elif reason_name in _INVALID_REASONS:
        exit_code, error_class = PULL_EXIT_FEED_INVALID, "feed_invalid"
    else:
        exit_code, error_class = PULL_EXIT_OTHER, "other"
    return exit_code, {"error_class": error_class, "reason": reason_name,
                       "file": feed_file, "r2_code": code,
                       "exception": type(root).__name__}


def pull_feed_once() -> int:
    """One feed pull regardless of SCRAMBLER_ENABLED; one JSON line; exit code."""
    from services import scrambler_feed_pull as feed
    from services.scrambler_preflight import pulled_feed_problems

    out = {"schema": PULL_SCHEMA}
    try:
        result = feed.run_feed_pull_once()
    except Exception as error:  # every failure is classified, never echoed
        code, fields = classify_pull_error(error, feed.FEED_FILES)
        print(json.dumps({**out, "result": "error", **fields}, sort_keys=True, ensure_ascii=True))
        return code
    # Re-read what landed and check it against the manifest that landed with it.
    manifest, problems = pulled_feed_problems(feed.dest_dir())
    if manifest is None or problems:
        print(json.dumps({**out, "result": "error", "error_class": "feed_invalid",
                          "reason": "post_write_verify_failed", "file": None,
                          "r2_code": None, "exception": None},
                         sort_keys=True, ensure_ascii=True))
        return PULL_EXIT_FEED_INVALID
    print(json.dumps({**out, "result": "ok", "generated_at": manifest.generated_at,
                      "rows": manifest.rows, "sha_ok": True,
                      "warnings": list(result.warnings)},
                     sort_keys=True, ensure_ascii=True))
    return PULL_EXIT_OK


def _read_existing(path: Path) -> dict:
    # Never invoke the bootstrap-capable loader. Reuse its bounds, durable
    # lock and complete validator while reading only existing files.
    marker = core._created_marker_path(path)
    if not path.is_file() or not marker.is_file():
        raise core.ScramblerHistoryLost("scrambler_report_history_or_marker_missing")
    with core.store_lock(path):
        def read(file):
            with file.open("rb") as stream:
                raw = stream.read(core.SCRAMBLER_HISTORY_MAX_BYTES + 1)
            if len(raw) > core.SCRAMBLER_HISTORY_MAX_BYTES:
                raise core.ScramblerHistoryCorrupt("scrambler_report_bytes_exceeded")
            return json.loads(raw)
        created = read(marker)
        history = core._validate_history(read(path), path=path)
        if (not isinstance(created, dict)
                or created.get("schema") != core.SCRAMBLER_CREATED_SCHEMA
                or created.get("history") != path.name
                or created.get("createdAt") != history["createdAt"]):
            raise core.ScramblerHistoryCorrupt("scrambler_report_marker_invalid")
        return history


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", action="append", default=[], help="Repeat for explicit historical page IDs, even OFF")
    parser.add_argument("--history", help="Absolute existing history path; otherwise the core durable environment path")
    parser.add_argument("--now", help="Optional ISO report clock (UTC when no offset is supplied)")
    parser.add_argument("--preflight", action="store_true",
                        help="Check the Scrambler ON-path configuration; exit 1 naming every missing setting")
    parser.add_argument("--as-enabled", action="store_true",
                        help="With --preflight: check as if SCRAMBLER_ENABLED=1, plus proof of one verified feed pull")
    parser.add_argument("--pull-feed-once", action="store_true",
                        help="Pull the originality feed once now, regardless of SCRAMBLER_ENABLED; exit 0/3/4/5/6")
    args = parser.parse_args(argv)
    if args.as_enabled and not args.preflight:
        parser.error("--as-enabled needs --preflight")
    if args.pull_feed_once and args.preflight:
        parser.error("--pull-feed-once and --preflight are separate steps")
    if args.pull_feed_once:
        return pull_feed_once()
    if args.preflight and args.as_enabled:
        from services.scrambler_preflight import preflight_problems_as_enabled
        problems = preflight_problems_as_enabled()
        print(json.dumps({"schema": PREFLIGHT_SCHEMA, "ready": not problems,
                          "asEnabled": True, "problems": problems},
                         sort_keys=True, ensure_ascii=True))
        return 1 if problems else 0
    if args.preflight:
        from services.scrambler_preflight import preflight_problems
        problems = preflight_problems(full=True)
        print(json.dumps({"schema": PREFLIGHT_SCHEMA, "ready": not problems,
                          "problems": problems}, sort_keys=True, ensure_ascii=True))
        return 1 if problems else 0
    moment = args.now or datetime.now(timezone.utc).isoformat()
    result = {"schema": SCHEMA, "scope": "lab_rendered_last_24h",
              "asOf": moment, "windowSeconds": 86400,
              "pages": {}, "alerts": []}
    pages = []
    try:
        core._validate_iso(moment, field="report_now")
        if args.page:
            if any(not page.strip() for page in args.page):
                raise ValueError("scrambler_report_page_invalid")
            pages = sorted({page.strip() for page in args.page})
        else:
            for page in sorted(core._page_list(os.environ.get(core.SCRAMBLER_PAGES_ENV))):
                enabled, reason = core.scrambler_gate(page)
                if enabled:
                    pages.append(page)
                elif reason not in {"scrambler_disabled", "scrambler_deny_listed", "scrambler_not_listed"}:
                    result["alerts"].append({"pageId": page, "kind": "unavailable", "reason": reason})
        if result["alerts"]:
            code = 2
        elif not pages:
            code = 0  # OFF/rollback never touches the history path.
        else:
            path = Path(args.history) if args.history else core.scrambler_history_path()
            if not path.is_absolute():
                raise ValueError("scrambler_report_history_path_invalid")
            history = _read_existing(path)
            for page in pages:
                report = core.scrambler_daily_report(history["pages"].get(page, []), now=moment)
                result["pages"][page] = report
                if (report["repeatCombos"] or report["consecutiveLengthRepeats"]
                        or report["consecutiveTreatmentRepeats"]):
                    result["alerts"].append({"pageId": page, "kind": "repeat"})
                if report["zeroVarietyCuts"]:
                    result["alerts"].append({"pageId": page, "kind": "zero"})
            code = 1 if result["alerts"] else 0
    except Exception as error:
        result["pages"] = {}
        result["alerts"] = [{"kind": "unavailable", "reason": str(error),
                             **({"pageId": page} if page else {})}
                            for page in (pages or [None])]
        code = 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
