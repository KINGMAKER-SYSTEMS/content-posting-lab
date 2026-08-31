"""Authenticated creative-ingredient projection for the Dossier editor."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

from routers.control_plane_recipes import LANE, require_control_plane_bearer
from services.dossier_ingredients import build_dossier_ingredient_catalog


REQUEST_SCHEMA = "content-lab.dossier-ingredient-request.v1"
SAFE_PAGE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
BODY_FIELDS = {"schema", "masterPages", "masterPagesHash"}

router = APIRouter()


@router.post("/v1/dossier-ingredients")
def dossier_ingredients(
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
    x_rt_lane: str = Header(alias="X-RT-Lane"),
    x_rt_page_id: str = Header(alias="X-RT-Page-Id"),
):
    require_control_plane_bearer(authorization)
    if x_rt_lane != LANE:
        raise HTTPException(400, "dossier ingredient lane mismatch")
    if not SAFE_PAGE_ID.fullmatch(x_rt_page_id):
        raise HTTPException(400, "X-RT-Page-Id is invalid")
    if not isinstance(body, dict) or set(body) != BODY_FIELDS:
        raise HTTPException(400, "dossier ingredient request fields do not match the contract")
    if body.get("schema") != REQUEST_SCHEMA:
        raise HTTPException(400, "dossier ingredient request schema mismatch")
    try:
        return build_dossier_ingredient_catalog(
            x_rt_page_id,
            body.get("masterPages"),
            body.get("masterPagesHash"),
        )
    except (OSError, ValueError) as error:
        raise HTTPException(409, str(error)) from error
