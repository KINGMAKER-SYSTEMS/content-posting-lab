import pytest

from services.caption_discipline import validate_caption_discipline


OFF_DUTY = "acct:user8696130597870"
OUTFIT = {"outfit-faceless": 1}
EMPTY = {"captionSet": None, "register": [], "slingshotShare": None}


def test_only_off_duty_outfit_accepts_the_empty_caption_selection():
    assert validate_caption_discipline(EMPTY, page_id=OFF_DUTY, format_mix=OUTFIT) == EMPTY
    with pytest.raises(ValueError, match="no caption library"):
        validate_caption_discipline(
            {**EMPTY, "captionSet": "meme", "register": ["relatable_meme"]},
            page_id=OFF_DUTY, format_mix=OUTFIT,
        )


@pytest.mark.parametrize(
    "page_id, format_mix",
    [
        ("acct:user3771297960159", OUTFIT),
        (OFF_DUTY, {"flower-morph": 1}),
        (OFF_DUTY, {"outfit-faceless": True}),
        (OFF_DUTY, {"outfit-faceless": 0.75, "flower-morph": 0.25}),
    ],
)
def test_other_recipe_contexts_still_require_a_caption_library(page_id, format_mix):
    with pytest.raises(ValueError, match="lowercase corpus slug"):
        validate_caption_discipline(EMPTY, page_id=page_id, format_mix=format_mix)


def test_glass_captionless_scope_matches_the_architecture_recipe():
    page = "acct:user9795207473816"
    glass = {"glass-architecture": 1}

    assert validate_caption_discipline(EMPTY, page_id=page, format_mix=glass) == EMPTY
    with pytest.raises(ValueError, match="Glass Signal"):
        validate_caption_discipline(
            {**EMPTY, "captionSet": "meme", "register": ["relatable_meme"]},
            page_id=page, format_mix=glass,
        )

    for foreign_page, format_mix in [
        (OFF_DUTY, glass),
        ("acct:user3771297960159", glass),
        (page, {"glass-architecture": True}),
        (page, {"glass-architecture": 0.75, "flower-morph": 0.25}),
    ]:
        with pytest.raises(ValueError, match="lowercase corpus slug"):
            validate_caption_discipline(EMPTY, page_id=foreign_page, format_mix=format_mix)
