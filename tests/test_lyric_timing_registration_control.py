"""Burnt's retired lyric registration must be refused before publication."""

import pytest

from services.control_plane_lyric_video import validate_lyric_recipe, validate_lyric_wire_plan
from tests.test_control_plane_lyric_video import PAGE, spec


def test_v1_public_registration_refuses_retired_burnt_lyric_lane():
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        validate_lyric_recipe(spec())


def test_unadvertised_lyric_timing_control_is_refused_before_catalog_lookup():
    value = spec()
    value["production"]["controls"]["lyricTimingSetId"] = "foreign-unregistered-set"
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        validate_lyric_recipe(value)


def test_capabilities_cannot_reenable_retired_burnt_lyric_publication():
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        validate_lyric_recipe(spec())


def test_reserved_legacy_lyric_wire_plan_is_refused():
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        validate_lyric_wire_plan({}, PAGE)
