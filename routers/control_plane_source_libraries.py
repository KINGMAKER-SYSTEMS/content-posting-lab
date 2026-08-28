"""Authenticated Content Lab source-library commissioning routes."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from routers.control_plane_recipes import LANE, require_control_plane_bearer
from services.control_plane_source_libraries import (
    SourceLibraryError,
    finalize_source_library,
    load_source_library_manifest,
    source_library_status,
    stage_source_clip,
)


router = APIRouter()


def _manifest(library_id: str):
    try:
        return load_source_library_manifest(library_id)
    except SourceLibraryError as error:
        raise HTTPException(error.status_code, error.detail) from error


def _mutation_headers(manifest, lane: str, supplied_hash: str) -> None:
    if lane != LANE:
        raise HTTPException(400, "source library lane mismatch")
    expected = "sha256:" + manifest.sha256
    if supplied_hash != expected:
        raise HTTPException(409, "source library manifest hash mismatch")


@router.get("/v1/source-libraries/{library_id}")
def source_library(
    library_id: str,
    authorization: str | None = Header(default=None),
):
    require_control_plane_bearer(authorization)
    try:
        return source_library_status(_manifest(library_id))
    except SourceLibraryError as error:
        raise HTTPException(error.status_code, error.detail) from error


@router.put("/v1/source-libraries/{library_id}/clips/{clip_sha256}")
async def upload_source_clip(
    library_id: str,
    clip_sha256: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_rt_lane: str = Header(alias="X-RT-Lane"),
    x_source_manifest_sha256: str = Header(alias="X-Source-Manifest-Sha256"),
    content_length: str | None = Header(default=None, alias="Content-Length"),
):
    require_control_plane_bearer(authorization)
    manifest = _manifest(library_id)
    _mutation_headers(manifest, x_rt_lane, x_source_manifest_sha256)
    if content_length is None:
        raise HTTPException(411, "Content-Length is required")
    try:
        length = int(content_length)
    except ValueError as error:
        raise HTTPException(400, "Content-Length must be an integer") from error
    if length < 0:
        raise HTTPException(400, "Content-Length must not be negative")
    try:
        return await stage_source_clip(
            manifest,
            clip_sha256,
            request.stream(),
            content_length=length,
        )
    except SourceLibraryError as error:
        raise HTTPException(error.status_code, error.detail) from error


@router.post("/v1/source-libraries/{library_id}/finalize")
def finalize(
    library_id: str,
    authorization: str | None = Header(default=None),
    x_rt_lane: str = Header(alias="X-RT-Lane"),
    x_source_manifest_sha256: str = Header(alias="X-Source-Manifest-Sha256"),
):
    require_control_plane_bearer(authorization)
    manifest = _manifest(library_id)
    _mutation_headers(manifest, x_rt_lane, x_source_manifest_sha256)
    try:
        return finalize_source_library(manifest)
    except SourceLibraryError as error:
        raise HTTPException(error.status_code, error.detail) from error
