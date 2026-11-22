"""The arithmetic: the room's range and the curve from noise to volume."""
import pytest

from ovos_audio_transformer_plugin_autovolume.curve import Curve, MIN_SPAN_DB, RoomRange, isotonic

DAY = 86400.0
SPAN = (-60.0, -30.0)


def test_room_range_is_learned_from_what_was_measured():
    room = RoomRange()
    assert room.span() is None
    for i in range(100):
        room.add(-60 + 30 * i / 99, now=1000.0 + i)
    quiet, noisy = room.span()
    assert -59 <= quiet <= -55
    assert -35 <= noisy <= -31


def test_a_steady_room_still_gets_a_span():
    room = RoomRange()
    for i in range(50):
        room.add(-45.2, now=float(i))
    quiet, noisy = room.span()
    assert noisy - quiet >= MIN_SPAN_DB


def test_the_room_forgets_slowly():
    room = RoomRange(half_life_days=1)
    for i in range(50):
        room.add(-70, now=float(i))
    for i in range(50):                 # a month later the room is noisier
        room.add(-40, now=30 * DAY + i)
    quiet, _ = room.span()
    assert quiet > -50


def test_default_curve_rises_softly_from_low_to_high():
    c = Curve(low=0.35, high=0.75)
    assert c.volume(-70, SPAN, 0) == 0.35
    assert c.volume(-20, SPAN, 0) == 0.75
    vols = [c.volume(x, SPAN, 0) for x in range(-60, -29)]
    assert vols == sorted(vols)
    assert abs(c.volume(-45, SPAN, 0) - 0.55) < 1e-9


def test_nothing_to_go_on():
    assert Curve().volume(-50, None, 0) is None


def test_the_curve_goes_through_my_points():
    c = Curve(min_points=2)
    c.learn(-55, 0.20, now=0)
    c.learn(-35, 0.90, now=0)
    assert abs(c.volume(-55, SPAN, 0) - 0.20) < 0.05
    assert abs(c.volume(-35, SPAN, 0) - 0.90) < 0.05
    mid = c.volume(-45, SPAN, 0)
    assert 0.3 < mid < 0.8


def test_one_point_is_not_enough():
    c = Curve(min_points=2)
    c.learn(-50, 0.1, now=0)
    assert c.volume(-50, SPAN, 0) == c.default(-50, SPAN)


def test_the_curve_never_falls_with_the_noise():
    c = Curve(min_points=2)
    c.learn(-55, 0.70, now=0)           # contradicting choices
    c.learn(-35, 0.30, now=0)
    vols = [c.volume(x, SPAN, 0) for x in range(-70, -20)]
    assert all(b >= a - 1e-9 for a, b in zip(vols, vols[1:]))


def test_a_new_point_replaces_one_close_by():
    c = Curve()
    c.learn(-50.0, 0.3, now=0)
    c.learn(-48.5, 0.6, now=1)
    assert len(c.points) == 1
    assert c.points[0].volume == 0.6
    c.learn(-40.0, 0.7, now=2)
    assert len(c.points) == 2


def test_newer_points_count_more():
    c = Curve(min_points=2, merge_db=0.1)
    c.learn(-45.0, 0.80, now=0)                 # old habit
    c.learn(-44.0, 0.30, now=60 * DAY)          # new speaker: quieter, and they conflict
    v = c.volume(-44.5, SPAN, now=60 * DAY)
    assert v < 0.5


def test_old_points_are_dropped_beyond_max():
    c = Curve(max_points=3, merge_db=0.5)
    for i in range(5):
        c.learn(-60 + 5 * i, 0.5, now=i)
    assert len(c.points) == 3
    assert min(p.time for p in c.points) == 2


def test_forget():
    c = Curve()
    c.learn(-50, 0.5, 0)
    c.forget()
    assert c.points == []


def test_isotonic_pools_violators():
    assert isotonic([1, 2, 3], [0.5, 0.3, 0.9], [1, 1, 1]) == pytest.approx([0.4, 0.4, 0.9])
    assert isotonic([1, 2], [0.8, 0.2], [3, 1]) == pytest.approx([0.65, 0.65])


def test_my_points_beyond_the_defaults_are_not_pulled_back():
    c = Curve(low=0.35, high=0.75, min_points=2)
    c.learn(-55, 0.10, now=0)       # quieter than the default low
    c.learn(-35, 0.95, now=0)       # louder than the default high
    assert c.volume(-55, SPAN, 0) == pytest.approx(0.10)
    assert c.volume(-35, SPAN, 0) == pytest.approx(0.95)
    assert c.volume(-80, SPAN, 0) == pytest.approx(0.10)
