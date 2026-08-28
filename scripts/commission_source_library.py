#!/usr/bin/env python3
"""Verify and optionally commission an immutable 8791 source library.

Dry-run is the default and performs no HTTP writes. ``--apply`` uploads only
manifest-listed bytes, then requests the server's all-or-nothing finalization.
The bearer token is accepted only from the environment or standard input so it
does not appear in shell history or process arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_DIR = REPO_ROOT / "recipes/source-libraries"
LANE = "content-bucket-control-plane"


class CommissioningError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> tuple[bytes, dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise CommissioningError(f"cannot read source manifest: {error}") from error
    if not isinstance(value, dict):
        raise CommissioningError("source manifest must be a JSON object")
    required = {"schema", "libraryId", "format", "authority", "clips"}
    if set(value) != required:
        raise CommissioningError("source manifest fields do not match the contract")
    if value.get("schema") != "content-lab.source-library-manifest.v1":
        raise CommissioningError("source manifest schema mismatch")
    if not isinstance(value.get("clips"), list) or not value["clips"]:
        raise CommissioningError("source manifest has no clips")
    return raw, value, hashlib.sha256(raw).hexdigest()


def verify_local_source(
    manifest_path: Path,
    rail_root: Path,
) -> tuple[dict[str, Any], str, str, list[tuple[dict[str, Any], Path]]]:
    _, manifest, manifest_sha = _load_manifest(manifest_path)
    authority = manifest.get("authority")
    if not isinstance(authority, dict) or authority.get("system") != "8791":
        raise CommissioningError("source manifest is not bound to 8791")
    clip_bank_rel = authority.get("clipBankPath")
    if clip_bank_rel != "out/clip_bank.json":
        raise CommissioningError("source manifest clip-bank path is not canonical")
    root = rail_root.resolve()
    clip_bank_path = (root / clip_bank_rel).resolve()
    if root not in clip_bank_path.parents or not clip_bank_path.is_file():
        raise CommissioningError("canonical clip bank is missing or escaped the Rail root")
    expected_bank_hash = authority.get("clipBankSha256")
    if (
        not isinstance(expected_bank_hash, str)
        or len(expected_bank_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_bank_hash)
    ):
        raise CommissioningError("source manifest clip-bank snapshot hash is invalid")
    try:
        clip_bank_raw = clip_bank_path.read_bytes()
        actual_bank_hash = hashlib.sha256(clip_bank_raw).hexdigest()
        clip_bank = json.loads(clip_bank_raw)
    except (OSError, ValueError) as error:
        raise CommissioningError("canonical clip bank is not readable JSON") from error
    if not isinstance(clip_bank, dict):
        raise CommissioningError("canonical clip bank must be keyed by SHA-256")

    verified: list[tuple[dict[str, Any], Path]] = []
    seen: set[str] = set()
    format_slug = manifest.get("format")
    for index, clip in enumerate(manifest["clips"]):
        if not isinstance(clip, dict):
            raise CommissioningError(f"manifest clip {index} is not an object")
        sha256 = clip.get("sha256")
        if not isinstance(sha256, str) or sha256 in seen:
            raise CommissioningError(f"manifest clip {index} has an invalid or duplicate SHA")
        seen.add(sha256)
        row = clip_bank.get(sha256)
        if not isinstance(row, dict):
            raise CommissioningError(f"clip {sha256} is absent from the canonical clip bank")
        qa = row.get("qa_sweep")
        if (
            row.get("format") != format_slug
            or row.get("eligible") is not True
            or row.get("text_scan") != "pass"
            or not isinstance(qa, dict)
            or qa.get("verdict") != "pass"
            or not isinstance(qa.get("swept_by"), str)
            or not qa["swept_by"].startswith("operator:8791")
            or row.get("file") != clip.get("railPath")
        ):
            raise CommissioningError(f"clip {sha256} no longer satisfies 8791 authority")
        source_path = (root / str(clip.get("railPath") or "")).resolve()
        if root not in source_path.parents or not source_path.is_file():
            raise CommissioningError(f"clip {sha256} source bytes are missing or escaped Rail")
        size = source_path.stat().st_size
        if size != clip.get("bytes") or _sha256_file(source_path) != sha256:
            raise CommissioningError(f"clip {sha256} bytes do not match the manifest")
        verified.append((clip, source_path))
    return manifest, manifest_sha, actual_bank_hash, verified


def _request_json(
    url: str,
    *,
    token: str,
    method: str = "GET",
    manifest_sha: str | None = None,
    data: BinaryIO | None = None,
    content_length: int | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if manifest_sha is not None:
        headers["X-RT-Lane"] = LANE
        headers["X-Source-Manifest-Sha256"] = "sha256:" + manifest_sha
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
        headers["Content-Type"] = "application/octet-stream"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=300) as response:
            body = response.read()
    except HTTPError as error:
        detail = error.read(4096).decode("utf-8", errors="replace")
        raise CommissioningError(
            f"Content Lab returned HTTP {error.code}: {detail}"
        ) from error
    except URLError as error:
        raise CommissioningError(f"Content Lab request failed: {error.reason}") from error
    try:
        value = json.loads(body)
    except ValueError as error:
        raise CommissioningError("Content Lab returned non-JSON data") from error
    if not isinstance(value, dict):
        raise CommissioningError("Content Lab returned a non-object response")
    return value


def _token(args: argparse.Namespace) -> str:
    if args.token_stdin:
        token = sys.stdin.readline().rstrip("\r\n")
    else:
        token = os.environ.get("CONTROL_PLANE_TOKEN", "")
    if not token:
        raise CommissioningError(
            "CONTROL_PLANE_TOKEN is required through the environment or --token-stdin"
        )
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("library_id")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--rail-root", type=Path, default=Path("/Users/Shared/rt-rail-repo"))
    parser.add_argument("--origin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args(argv)
    manifest_path = args.manifest or DEFAULT_MANIFEST_DIR / f"{args.library_id}.json"
    manifest, manifest_sha, current_bank_sha, verified = verify_local_source(
        manifest_path, args.rail_root,
    )
    if manifest.get("libraryId") != args.library_id:
        raise CommissioningError("requested library id does not match the manifest")

    summary = {
        "libraryId": args.library_id,
        "manifestSha256": "sha256:" + manifest_sha,
        "recipeVersion": "v" + manifest_sha[:12],
        "clipBankSnapshotSha256": "sha256:" + manifest["authority"]["clipBankSha256"],
        "clipBankCurrentSha256": "sha256:" + current_bank_sha,
        "clipBankSnapshotMatchesCurrent": (
            manifest["authority"]["clipBankSha256"] == current_bank_sha
        ),
        "clipCount": len(verified),
        "totalBytes": sum(int(clip["bytes"]) for clip, _ in verified),
        "mode": "apply" if args.apply else "dry-run",
    }
    if not args.apply:
        print(json.dumps(summary, sort_keys=True))
        return 0
    if not args.origin:
        raise CommissioningError("--origin is required with --apply")
    origin = args.origin.rstrip("/")
    if not origin.startswith("https://"):
        raise CommissioningError("--origin must use https://")
    token = _token(args)
    encoded_id = quote(args.library_id, safe="")
    base = f"{origin}/api/control-plane/v1/source-libraries/{encoded_id}"
    status = _request_json(base, token=token)
    if status.get("manifestSha256") != "sha256:" + manifest_sha:
        raise CommissioningError("deployed Content Lab manifest hash does not match local bytes")
    present = set(status.get("stagedClipSha256") or []) | set(
        status.get("finalClipSha256") or []
    )
    for clip, source_path in verified:
        if clip["sha256"] in present:
            continue
        with source_path.open("rb") as handle:
            status = _request_json(
                f"{base}/clips/{clip['sha256']}",
                token=token,
                method="PUT",
                manifest_sha=manifest_sha,
                data=handle,
                content_length=int(clip["bytes"]),
            )
    # Uploading a large library can span an operator change. Re-read the live
    # authority and every source byte before making prompts.json visible.
    final_manifest, final_sha, _, final_verified = verify_local_source(
        manifest_path, args.rail_root,
    )
    if (
        final_sha != manifest_sha
        or final_manifest.get("libraryId") != args.library_id
        or [clip["sha256"] for clip, _ in final_verified]
        != [clip["sha256"] for clip, _ in verified]
    ):
        raise CommissioningError("source authority changed during commissioning")
    status = _request_json(
        f"{base}/finalize",
        token=token,
        method="POST",
        manifest_sha=manifest_sha,
        data=b"",
        content_length=0,
    )
    if status.get("finalized") is not True:
        raise CommissioningError("Content Lab did not certify source-library finalization")
    summary["finalized"] = True
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommissioningError as error:
        print(f"commissioning refused: {error}", file=sys.stderr)
        raise SystemExit(1)
