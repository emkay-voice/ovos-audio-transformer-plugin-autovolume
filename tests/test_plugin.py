"""The plugin on a FakeBus: measuring, learning, keeping its hands off, smooth steps."""
import json
import threading
import time

import numpy as np
import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

import ovos_audio_transformer_plugin_autovolume as av

RATE = 16000
CHUNK = 1280                    # 80 ms


def tone(db: float, n: int = CHUNK) -> bytes:
    """A chunk of noise at about ``db`` dBFS."""
    rng = np.random.default_rng(int(-db * 10))
    x = rng.standard_normal(n) * (10 ** (db / 20))
    return np.clip(x * 32768, -32768, 32767).astype(np.int16).tobytes()


def feed(p, db: float, seconds: float):
    """Measure directly (as the worker thread does), for exact tests."""
    c = tone(db)
    for _ in range(int(seconds * RATE / CHUNK)):
        p.measure(c)


@pytest.fixture
def make(tmp_path):
    made = []

    def _make(**cfg):
        config = {"state_file": str(tmp_path / "autovolume.json"), "step_seconds": 5,
                  "window_seconds": 30, "hold_seconds": 60, "tail_seconds": 2}
        config.update(cfg)
        p = av.AutoVolumePlugin(config)
        bus = FakeBus()
        sets = []
        bus.on("mycroft.volume.set", lambda m: sets.append(m))
        p.bind(bus)
        p.sets = sets
        made.append(p)
        bus.emit(Message("mycroft.volume.get.response", {"percent": 0.5, "muted": False}))
        return p
    yield _make
    for p in made:
        p.default_shutdown()


def own_sets(p):
    return [m.data["percent"] for m in p.sets if m.context.get("source") == av.PLUGIN_ID]


def learn_room(p):
    """A room that has been quiet (-60) and noisy (-30) for days."""
    now = time.time()
    for i in range(40):
        p.room.add(-60 if i % 2 else -30, now=now, weight=1000)


# ---- measuring --------------------------------------------------------------

def test_audio_passes_through_untouched(make):
    p = make()
    c = tone(-40)
    assert p.on_audio(c) is c


def test_measures_the_level(make):
    p = make()
    feed(p, -40, 20)
    assert abs(p.level_db() - (-40)) < 1.5
    assert p.warm()


def test_never_blocks_the_microphone(make):
    """Even with the worker stuck, on_audio returns at once and drops what it can't queue."""
    p = make()
    stuck = threading.Event()
    p.measure = lambda chunk: stuck.wait()
    c = tone(-40)
    p.on_audio(c)                           # the worker takes this one and hangs
    start = time.monotonic()
    for _ in range(1000):
        assert p.on_audio(c) is c
    assert time.monotonic() - start < 0.5
    stuck.set()


def test_the_worker_thread_measures(make):
    p = make()
    for _ in range(50):
        p.on_audio(tone(-35))
    end = time.monotonic() + 2
    while p.level_db() is None and time.monotonic() < end:
        time.sleep(0.01)
    assert p.level_db() is not None


def test_inline_without_threads(make):
    """Where there are no threads (Pyodide) it measures and steps as the audio arrives."""
    p = make(inline=True)
    assert p._threads == []
    ticks = []
    p.tick = lambda: ticks.append(1)
    for _ in range(50):
        p.on_audio(tone(-35))
    assert p.level_db() is not None
    assert ticks == [1]                     # the first chunk steps, the next only after step_seconds


def test_inline_by_default_only_without_threads(make, monkeypatch):
    assert make().inline is False
    monkeypatch.setattr(av, "NO_THREADS", True)
    assert make().inline is True


# ---- adjusting smoothly -----------------------------------------------------

def test_turns_up_in_a_noisy_room_slowly(make):
    p = make(max_step=0.02, deadband=0.03)
    learn_room(p)
    feed(p, -30, 30)                        # noisy now: target is high
    volumes = [p.tick(now=1000.0 + i) for i in range(5)]
    assert volumes == pytest.approx([0.52, 0.54, 0.56, 0.58, 0.60])
    assert own_sets(p) == pytest.approx(volumes)
    assert all(m.data["play_sound"] is False for m in p.sets)


def test_leaves_small_differences_alone(make):
    p = make(deadband=0.03)
    learn_room(p)
    feed(p, -45, 30)                        # middle of the room: target 0.55
    p._volume = 0.54
    assert p.tick(now=1000.0) is None
    assert own_sets(p) == []


def test_waits_until_it_has_measured_a_while(make):
    p = make(window_seconds=90)
    learn_room(p)
    feed(p, -30, 5)
    assert p.tick(now=1000.0) is None


def test_does_not_pump(make):
    """A room that flickers between loud and quiet gives a steady volume, not one that follows each burst."""
    p = make(window_seconds=60, max_step=0.02)
    learn_room(p)
    for i in range(300):                    # the room has been like this for a while
        feed(p, -30 if i % 2 else -60, 1)
    p._volume = p.target()
    vols = []
    for i in range(20):
        feed(p, -30 if i % 2 else -60, 1)
        vols.append(p.tick(now=1000.0 + i) or p._volume)
    assert max(vols) - min(vols) <= 0.04


# ---- hands off --------------------------------------------------------------

@pytest.mark.parametrize("start,end", [
    ("recognizer_loop:audio_output_start", "recognizer_loop:audio_output_end"),
    ("recognizer_loop:record_begin", "recognizer_loop:record_end"),
])
def test_pauses_while_speaking_and_listening(make, start, end):
    p = make(tail_seconds=0.2)
    learn_room(p)
    feed(p, -30, 30)
    p.bus.emit(Message(start))
    assert p.paused()
    assert p.tick() is None
    level = p.level_db()
    feed(p, -10, 10)                        # its own voice: not measured
    assert p.level_db() == level
    p.bus.emit(Message(end))
    assert p.paused()                       # the tail
    time.sleep(0.25)
    assert not p.paused()
    assert p.tick() is not None


def test_pauses_while_playing(make):
    p = make(tail_seconds=0)
    learn_room(p)
    feed(p, -30, 30)
    p.bus.emit(Message("ovos.common_play.player.state", {"state": av.OCP_PLAYING}))
    assert p.tick() is None
    p.on_audio(tone(-5))
    assert p._queue.empty()                 # the music isn't even queued
    p.bus.emit(Message("ovos.common_play.player.state", {"state": 0}))
    assert p.tick() is not None


def test_pauses_while_muted(make):
    p = make()
    learn_room(p)
    feed(p, -30, 30)
    p.bus.emit(Message("mycroft.volume.get.response", {"percent": 0.5, "muted": True}))
    assert p.tick(now=1000.0) is None


def test_speech_only_gives_media_back_my_volume(make):
    p = make(tail_seconds=0, speech_only=True)
    learn_room(p)
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.4}))     # me, before it warmed up
    p._hold_until = 0
    feed(p, -30, 30)
    p.tick(now=1000.0)
    p.tick(now=1001.0)
    assert p._volume == pytest.approx(0.44)
    p.bus.emit(Message("ovos.common_play.player.state", {"state": av.OCP_PLAYING}))
    assert own_sets(p)[-1] == pytest.approx(0.4)


def test_without_speech_only_media_keeps_the_room_volume(make):
    p = make(tail_seconds=0, speech_only=False)
    learn_room(p)
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.4}))
    p._hold_until = 0
    feed(p, -30, 30)
    p.tick(now=1000.0)
    n = len(own_sets(p))
    p.bus.emit(Message("ovos.common_play.player.state", {"state": av.OCP_PLAYING}))
    assert len(own_sets(p)) == n


# ---- learning from me -------------------------------------------------------

def test_learns_when_i_set_the_volume(make):
    p = make(min_points=2)
    learn_room(p)
    feed(p, -55, 30)
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.15}))
    feed(p, -32, 60)
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.95}))
    assert len(p.curve.points) == 2
    feed(p, -55, 400)
    assert p.target() < 0.3                 # my quiet choice, not the default 0.35+
    feed(p, -32, 400)
    assert p.target() > 0.85


def test_its_own_changes_are_not_learned(make):
    p = make()
    learn_room(p)
    feed(p, -30, 30)
    p.tick(now=1000.0)
    assert own_sets(p)
    assert p.curve.points == []


def test_a_manual_change_wins_for_a_while(make):
    p = make(hold_seconds=60)
    learn_room(p)
    feed(p, -30, 30)
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.2}))
    now = time.monotonic()
    assert p.tick(now=now + 1) is None
    assert p.tick(now=now + 61) is not None


def test_learns_from_volume_up_via_the_answer(make):
    p = make()
    learn_room(p)
    feed(p, -40, 30)
    p.bus.emit(Message("mycroft.volume.increase", {}))
    p.bus.emit(Message("mycroft.volume.get.response", {"percent": 0.7, "muted": False}))
    assert [(round(pt.volume, 2)) for pt in p.curve.points] == [0.7]


def test_not_learned_while_playing(make):
    p = make(tail_seconds=0)
    learn_room(p)
    feed(p, -40, 30)
    p.bus.emit(Message("ovos.common_play.player.state", {"state": av.OCP_PLAYING}))
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.9}))
    assert p.curve.points == []
    assert p._manual_volume == 0.9


def test_points_are_kept_on_disk(make, tmp_path):
    p = make()
    learn_room(p)
    feed(p, -40, 30)
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.6}))
    data = json.loads((tmp_path / "autovolume.json").read_text())
    assert len(data["points"]) == 1
    again = make()
    assert len(again.curve.points) == 1


# ---- bus commands -----------------------------------------------------------

def test_off_and_on(make):
    p = make()
    learn_room(p)
    feed(p, -30, 30)
    r = p.bus.wait_for_response(Message("ovos.autovolume.disable"))
    assert r.data["enabled"] is False
    assert p.tick(now=1000.0) is None
    r = p.bus.wait_for_response(Message("ovos.autovolume.enable"))
    assert r.data["enabled"] is True
    assert p.tick(now=1000.0) is not None


def test_off_is_remembered(make):
    p = make()
    p.bus.wait_for_response(Message("ovos.autovolume.disable"))
    assert make().enabled is False


def test_forget(make):
    p = make()
    learn_room(p)
    feed(p, -40, 30)
    p.bus.emit(Message("mycroft.volume.set", {"percent": 0.6}))
    r = p.bus.wait_for_response(Message("ovos.autovolume.forget"))
    assert r.data["points"] == 0
    assert p.curve.points == [] and p.room.span() is None


def test_status(make):
    p = make()
    learn_room(p)
    feed(p, -40, 30)
    r = p.bus.wait_for_response(Message("ovos.autovolume.status"))
    d = r.data
    assert d["enabled"] and d["speech_only"]
    assert d["points"] == 0 and d["learning"] is False
    assert d["quiet_db"] < d["level_db"] < d["noisy_db"]
    assert 0.35 <= d["target"] <= 0.75
