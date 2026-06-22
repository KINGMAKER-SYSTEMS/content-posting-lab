from routers.slideshow import safe_slug


def test_strips_and_replaces_unsafe_chars():
    assert safe_slug("  My Format!  ") == "My_Format_"


def test_keeps_allowed_chars():
    assert safe_slug("a-b_C9") == "a-b_C9"


def test_truncates_to_64():
    assert len(safe_slug("x" * 100)) == 64


def test_all_unsafe_becomes_underscores():
    assert safe_slug("@#$ ") == "___"


def test_empty_after_strip():
    assert safe_slug("   ") == ""


def test_matches_legacy_regex_behavior():
    import re

    for name in ["Hello World", "café/menu", "a" * 80, "  trimmed  ", "ok-name_1"]:
        legacy = re.sub(r"[^a-zA-Z0-9_\-]", "_", name.strip())[:64]
        assert safe_slug(name) == legacy
