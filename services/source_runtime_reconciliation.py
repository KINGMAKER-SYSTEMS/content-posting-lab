"""Explicit reconciliation after an operator proves a source runtime stopped.

No production paths or platform liveness are inferred. Dry runs are read-only;
apply compares one reviewed job revision and preserves all existing media.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading

from services.generation_recovery import runner_lock, store_lock
from services.job_token_rotation import (
    JOB_ID_RE, REVISION_RE, RotationRefused, _load_strict, _revision,
)
from services.json_store import atomic_save


class ReconciliationRefused(RotationRefused):
    pass


def _check_write_directory(path):
    directory = path.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
            or directory.st_mode & 0o022):
        raise ReconciliationRefused("store_directory_not_owned")
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    if os.path.lexists(temporary):
        raise ReconciliationRefused("store_temp_conflict")


def _metadata(store, job_id, retired_runtime):
    if not JOB_ID_RE.fullmatch(job_id):
        raise ReconciliationRefused("job_id_invalid")
    if not isinstance(retired_runtime, str) or not retired_runtime.strip():
        raise ReconciliationRefused("retired_runtime_required")
    job = store["jobs"].get(job_id)
    if not isinstance(job, dict):
        return {"status": "not_found_or_archived", "jobId": job_id}
    if job.get("jobId") != job_id:
        raise ReconciliationRefused("job_identity_invalid")
    if job.get("runtimeId") != retired_runtime:
        return {"status": "runtime_not_selected", "jobId": job_id}
    kind = job.get("sourceKind")
    if kind not in {"dossier_source_dna", "page_source_import"}:
        return {"status": "not_a_source_job", "jobId": job_id}
    restartable = (kind == "page_source_import" and job.get("status") == "failed"
                   and job.get("error") == "source_import_runtime_restarted")
    if job.get("status") not in {"queued", "running"} and not restartable:
        return {"status": "terminal_history_unchanged", "jobId": job_id}
    marker = "sourceClaimVersion" if kind == "dossier_source_dna" else "sourceImportClaimVersion"
    if marker in job or (kind == "page_source_import" and "sourceImportAttemptId" in job):
        return {"status": "claimed_or_malformed_requires_normal_recovery", "jobId": job_id}
    if job.get("clips") != []:
        raise ReconciliationRefused("existing_artifacts_require_reconciliation")
    return {"status": "ready", "jobId": job_id, "sourceKind": kind,
            "jobStatus": job["status"], "jobRevisionSha256": _revision(job)}


def _legacy_import_parent(job, generation_root):
    root = Path(generation_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ReconciliationRefused("generation_root_unavailable")
    page = job.get("pageId")
    if not isinstance(page, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,199}", page):
        raise ReconciliationRefused("page_identity_invalid")
    parent = root / page / "source-imports" / job["jobId"]
    # Refuse links in the known layout even when their resolved destination
    # happens to remain under the configured generation root.
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ReconciliationRefused("artifact_root_invalid")
    value = job.get("artifactRoot")
    if not isinstance(value, str) or Path(value).resolve() != parent.resolve():
        raise ReconciliationRefused("artifact_root_invalid")
    return parent


def inspect_job(store_path, job_id, *, retired_runtime, generation_root=None):
    """No lock files, media directories, tokens or job writes during dry run."""
    store = _load_strict(Path(store_path))
    result = _metadata(store, job_id, retired_runtime)
    if result["status"] == "ready" and result["sourceKind"] == "page_source_import":
        if generation_root is None:
            raise ReconciliationRefused("generation_root_required")
        _legacy_import_parent(store["jobs"][job_id], generation_root)
    return result


def list_legacy_jobs(store_path, *, limit=100):
    """List bounded, non-secret candidate identities before a controlled cutover."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ReconciliationRefused("limit_invalid")
    store = _load_strict(Path(store_path))
    rows, unknown, more = [], 0, False
    for job_id, job in store["jobs"].items():
        if not isinstance(job, dict) or job.get("sourceKind") not in {"dossier_source_dna", "page_source_import"}:
            continue
        if (job.get("status") not in {"queued", "running"}
                and not (job.get("sourceKind") == "page_source_import"
                         and job.get("status") == "failed"
                         and job.get("error") == "source_import_runtime_restarted")):
            continue
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            unknown += 1
            continue
        marker = "sourceClaimVersion" if job["sourceKind"] == "dossier_source_dna" else "sourceImportClaimVersion"
        if marker in job or (job["sourceKind"] == "page_source_import" and "sourceImportAttemptId" in job):
            valid = type(job.get(marker)) is int and job[marker] == 1
            if job["sourceKind"] == "page_source_import":
                valid = valid and isinstance(job.get("sourceImportAttemptId"), str) and bool(
                    re.fullmatch(r"[0-9a-f]{32}", job["sourceImportAttemptId"]))
            unknown += not valid
            continue
        runtime = job.get("runtimeId")
        if not isinstance(runtime, str) or not re.fullmatch(r"[0-9a-f]{32}", runtime):
            unknown += 1
            continue
        try:
            row = _metadata(store, job_id, runtime)
        except ReconciliationRefused:
            unknown += 1
            continue
        if row["status"] != "ready":
            continue
        if len(rows) == limit:
            more = True
            break
        rows.append({**row, "runtimeId": runtime})
    return {"status": "read_only_inventory", "jobs": rows,
            "hasMore": more, "unknownOrInvalid": unknown}


def railway_volume_proof(store_path, generation_root, *, environ=None,
                         mountinfo=None, command=None, device_for=None):
    """Verify the actual mounted-volume/entrypoint boundary, without API auth.

    Railway documents exclusive deployment attachment for a service volume:
    https://docs.railway.com/volumes/reference#caveats
    Runtime-provided volume identity/name and mount path are documented at:
    https://docs.railway.com/volumes#provided-variables
    Environment markers alone are insufficient: require the real block mount,
    matching device identities, no nested foreign filesystem, and main.py.
    """
    env = os.environ if environ is None else environ
    for key in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_ID",
                "RAILWAY_DEPLOYMENT_ID", "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_MOUNT_PATH"):
        if not isinstance(env.get(key), str) or not env[key].strip():
            return "platform_volume_identity_unavailable"
    try:
        volume = Path(env["RAILWAY_VOLUME_MOUNT_PATH"])
        if not volume.is_absolute() or volume.is_symlink():
            return "volume_path_invalid"
        if mountinfo is None:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        if command is None:
            command = Path("/proc/self/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        if (len(command) != 2 or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(command[0]).name)
                or Path(command[1]).resolve() != main_path):
            return "single_process_entrypoint_unverified"
        mounts = []
        for line in mountinfo.splitlines():
            before, after = line.split(" - ", 1)
            fields, filesystem = before.split(), after.split()
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
            mounts.append((mount, fields[2], filesystem[0], filesystem[1]))
        matches = [row for row in mounts if row[0] == volume]
        if len(matches) != 1:
            return "volume_not_mounted"
        mounted = matches[0]
        if mounted[2] not in {"ext4", "xfs"} or not mounted[3].startswith("/dev/"):
            return "volume_block_identity_unverified"
        major, minor = map(int, mounted[1].split(":"))
        if major == 0:
            return "volume_block_identity_unverified"
        for path in (Path(store_path).resolve(), Path(generation_root).resolve()):
            if not path.is_relative_to(volume.resolve()):
                return "source_data_outside_volume"
            covering = [row for row in mounts if path == row[0] or row[0] in path.parents]
            nearest = max(covering, key=lambda row: len(row[0].parts))
            if nearest != mounted:
                return "source_data_on_nested_filesystem"
            existing = path
            while not existing.exists():
                existing = existing.parent
            device = os.stat(existing).st_dev if device_for is None else device_for(existing)
            if (os.major(device), os.minor(device)) != (major, minor):
                return "source_data_device_mismatch"
    except (OSError, ValueError, IndexError, UnicodeError):
        return "runtime_mount_evidence_unavailable"
    return "verified_serial_railway_volume"


def reconcile_railway_startup(store_path, generation_root, current_runtime_id):
    """Reconcile bounded stopped legacy owners before serving any requests.

    Unverified deployments keep their original behavior and require the explicit
    CLI. This never blocks app startup or creates a new global posting gate.
    """
    try:
        proof = railway_volume_proof(store_path, generation_root)
    except Exception:
        return {"status": "skipped_unverified_runtime", "proof": "runtime_evidence_failed", "reconciled": 0}
    if proof != "verified_serial_railway_volume":
        return {"status": "skipped_unverified_runtime", "proof": proof, "reconciled": 0}
    if not isinstance(current_runtime_id, str) or not re.fullmatch(r"[0-9a-f]{32}", current_runtime_id):
        return {"status": "skipped_unknown_runtime", "reconciled": 0}
    path = Path(store_path)
    if not path.exists():
        return {"status": "no_legacy_state", "reconciled": 0}
    try:
        plan = list_legacy_jobs(path, limit=1000)
    except (RotationRefused, OSError, ValueError):
        return {"status": "manual_reconciliation_required", "reconciled": 0}
    changed, pending = 0, plan["unknownOrInvalid"]
    for row in plan["jobs"]:
        if row["runtimeId"] == current_runtime_id:
            pending += 1
            continue
        try:
            result = reconcile_job(path, row["jobId"], retired_runtime=row["runtimeId"],
                generation_root=generation_root, expected_job_sha256=row["jobRevisionSha256"],
                runtime_stopped_confirmed=True)
        except (RotationRefused, OSError, ValueError):
            pending += 1
            continue
        changed += result["status"] == "reconciled"
    return {"status": "reconciled_verified_runtime", "proof": proof,
            "reconciled": changed, "manual": pending, "hasMore": plan["hasMore"]}


def reconcile_job(store_path, job_id, *, retired_runtime, expected_job_sha256,
                  runtime_stopped_confirmed=False, generation_root=None):
    """Apply one reviewed legacy transition after external retirement proof.

    A free per-job lock alone does not prove an unmodified legacy runner died.
    The caller must first verify the old deployment/process no longer exists.
    """
    if not runtime_stopped_confirmed:
        raise ReconciliationRefused("runtime_retirement_not_confirmed")
    if not isinstance(expected_job_sha256, str) or not REVISION_RE.fullmatch(expected_job_sha256):
        raise ReconciliationRefused("expected_revision_invalid")
    path = Path(store_path)
    _load_strict(path)
    with store_lock(path):
        store = _load_strict(path)
        result = _metadata(store, job_id, retired_runtime)
        if result["status"] != "ready":
            return result
        if not secrets.compare_digest(result["jobRevisionSha256"], expected_job_sha256):
            raise ReconciliationRefused("job_changed_retry_dry_run")
        with runner_lock(path, job_id) as acquired:
            if not acquired:
                raise ReconciliationRefused("runner_still_active")
            _check_write_directory(path)
            job = store["jobs"][job_id]
            attempt_root = None
            if result["sourceKind"] == "page_source_import":
                if generation_root is None:
                    raise ReconciliationRefused("generation_root_required")
                parent = _legacy_import_parent(job, generation_root)
                attempt = secrets.token_hex(16)
                attempt_root = parent / attempt
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                attempt_root.mkdir(mode=0o700)
                job.update(sourceImportClaimVersion=1, sourceImportAttemptId=attempt,
                           artifactRoot=str(attempt_root))
            job.update(status="failed", completedAt=datetime.now(timezone.utc).isoformat(),
                       error=("source_import_runtime_restarted" if attempt_root is not None
                              else "generation_runtime_restarted"))
            previous_umask = os.umask(0o077)
            try:
                atomic_save(path, store)
            except Exception:
                if attempt_root is not None:
                    try:
                        attempt_root.rmdir()  # Only the empty directory created here.
                    except OSError:
                        pass
                raise
            finally:
                os.umask(previous_umask)
            return {"status": "reconciled", "jobId": job_id,
                    "sourceKind": result["sourceKind"], "jobRevisionSha256": _revision(job)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconcile one legacy source job after verified runtime retirement")
    parser.add_argument("--store", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--job-id")
    target.add_argument("--list", action="store_true", help="list legacy job/runtime IDs and revisions without media or credentials")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--retired-runtime")
    parser.add_argument("--generation-root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-job-sha256")
    parser.add_argument("--runtime-stopped-confirmed", action="store_true",
                        help="operator has verified the old deployment/process is stopped; a free lock is insufficient")
    args = parser.parse_args(argv)
    try:
        if args.list:
            if args.apply or args.expected_job_sha256 or args.runtime_stopped_confirmed:
                parser.error("--list is always read-only")
            result = list_legacy_jobs(args.store, limit=args.limit)
        elif args.apply:
            result = reconcile_job(args.store, args.job_id, retired_runtime=args.retired_runtime,
                                   generation_root=args.generation_root,
                                   expected_job_sha256=args.expected_job_sha256,
                                   runtime_stopped_confirmed=args.runtime_stopped_confirmed)
        else:
            if args.expected_job_sha256 or args.runtime_stopped_confirmed:
                parser.error("apply-only arguments require --apply")
            result = inspect_job(args.store, args.job_id, retired_runtime=args.retired_runtime,
                                 generation_root=args.generation_root)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RotationRefused, OSError, ValueError) as error:
        print(json.dumps({"status": "refused", "code": getattr(error, "code", "store_operation_failed")}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
