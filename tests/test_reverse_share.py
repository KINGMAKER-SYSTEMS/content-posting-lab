"""Reverse-share regression tests (services/clip_transforms.py).

Reverse is the least-used treatment (Eric 2026-10-28): ``REVERSE_SHARE`` was
cut from 1/4 to 1/15. On a single, unchained draw, mirror and motion are drawn
from their own independent seed parts, so changing the reverse threshold can
never move a mirror or a motion decision; that is the clean "reverse-only"
proof (the first-draw test below).

Production chains draws across cuts (``cut_transforms(page, slot, previous)``),
and the anti-repeat redraw re-rolls the whole combination on an exact
collision, so a changed reverse share also moves a small share of mirror/motion
decisions and lifts the delivered reverse rate from the 1/15 first draw to
~9%. The chained test below pins that delivered rate and that it stays under
the old 1/4 build's chained rate.
"""
import services.clip_transforms as ct

BTL = "acct:between-the-lines66"


def _slots(count: int) -> list[str]:
    """``count`` distinct, deterministic BTL cut slots (same shape the dossier
    source loop builds: ``master.sha256:start_ms:duration_ms``)."""
    return [f"{'a' * 64}:{index * 8000}:{5000 + (index % 10) * 400}" for index in range(count)]


def _draws(slots: list[str]) -> list[ct.CutTransforms]:
    """One seeded first-draw per slot; BTL is not on LOOP_SWAP_PAGES so
    ``loop_swap`` stays off on every draw."""
    return [ct.cut_transforms(BTL, slot, None) for slot in slots]


def _chained_draws(slots: list[str]) -> list[ct.CutTransforms]:
    """The production chain: each cut avoids the previous cut's combination."""
    previous = None
    draws = []
    for slot in slots:
        drawn = ct.cut_transforms(BTL, slot, previous)
        draws.append(drawn)
        previous = drawn
    return draws


def test_reverse_share_is_4_to_10_percent_over_1500_seeded_cuts():
    draws = _draws(_slots(1500))
    reverses = sum(1 for drawn in draws if drawn.reverse)
    share = reverses / len(draws)
    assert 0.04 <= share <= 0.10, (reverses, share)


def test_btl_first_draw_differs_from_old_build_only_by_fewer_reverses(monkeypatch):
    slots = _slots(1500)
    new_draws = _draws(slots)
    # The old build drew reverse at 1/4; everything else is unchanged on the
    # unchained first draw.
    monkeypatch.setattr(ct, "REVERSE_SHARE", 1 / 4)
    old_draws = _draws(slots)
    assert sum(1 for drawn in old_draws if drawn.reverse) > sum(1 for drawn in new_draws if drawn.reverse)
    for new, old in zip(new_draws, old_draws):
        assert new.mirror is old.mirror, "mirror is seeded independently and never moves"
        assert new.motion == old.motion, "motion is seeded independently and never moves"
        assert new.loop_swap is old.loop_swap
        assert not (new.reverse and not old.reverse), "a new reverse can only come from an old reverse"


def test_btl_delivered_reverse_share_is_about_nine_percent_when_chained(monkeypatch):
    slots = _slots(1500)
    delivered = _chained_draws(slots)
    reverses = sum(1 for drawn in delivered if drawn.reverse)
    share = reverses / len(delivered)
    # Chained across cuts, the anti-repeat redraw lifts the delivered reverse
    # rate from the 1/15 first draw to ~9.2% (139/1500); it must stay well
    # under the old 1/4 build's chained rate (~28.5%).
    assert 0.07 <= share <= 0.12, (reverses, share)
    monkeypatch.setattr(ct, "REVERSE_SHARE", 1 / 4)
    old = _chained_draws(slots)
    old_share = sum(1 for drawn in old if drawn.reverse) / len(old)
    assert share < old_share, (share, old_share)
