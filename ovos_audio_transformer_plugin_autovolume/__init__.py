"""Auto volume: speak up when the room is noisy, softly when it is quiet.

An OpenVoiceOS audio transformer. While the listener waits for the wake word it
hands the room audio to audio transformers (``on_audio``); this one only
measures how loud it is. Nothing is recorded, kept or sent: each chunk becomes
one number (its power) and is dropped.

From the room's running level (over about 90 seconds) it picks a volume on a
curve from a low to a high volume, and moves the volume there slowly, over the
bus (``mycroft.volume.set``), so whatever handles volume on the device does the
rest: a PHAL plugin on a Pi, the browser in Emkay.

It learns from you: when you set the volume yourself, that is a point (noise
now, the volume you chose), and the curve goes through your points. It keeps
its hands off while the device speaks, listens or plays something.

Off by default: OVOS only loads it when it is named in the
``audio_transformers`` config, and ``"active": false`` turns it off again.
"""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
from ovos_bus_client.message import Message
from ovos_plugin_manager.templates.transformers import AudioTransformer
from ovos_utils.log import LOG

from ovos_audio_transformer_plugin_autovolume.curve import Curve, RoomRange, clamp

PLUGIN_ID = "ovos-audio-transformer-plugin-autovolume"
#: OCP player state "playing" (ovos_utils.ocp.PlayerState.PLAYING)
OCP_PLAYING = 1
#: below this the level is just digital silence
SILENCE_DB = -100.0


def state_path() -> Path:
    from ovos_config.locations import get_xdg_data_save_path
    return Path(get_xdg_data_save_path()) / "autovolume" / "autovolume.json"


class AutoVolumePlugin(AudioTransformer):
    """Config (``audio_transformers.ovos-audio-transformer-plugin-autovolume``):

    Being named in that section is the opt-in; "active": false turns it off again (OVOS' rule).
    enabled (bool): start right away, default true. Turning it on or off on the bus is remembered.
    speech_only (bool): default true. When something starts playing, the volume goes back to the
        one you last set yourself, so music and stories keep their own volume; auto volume takes
        over again when it stops. With false, media plays at the room's volume too.
    low, high (float 0..1): default volumes for the quiet and the noisy end of the room, 0.35 / 0.75.
    window_seconds: the room's running level is over about this long, default 90.
    step_seconds: how often the volume is looked at, default 5.
    max_step: the most the volume moves per step, default 0.02.
    deadband: differences smaller than this are left alone, default 0.03.
    hold_seconds: after you set the volume yourself it is left alone this long, default 300.
    tail_seconds: measuring and adjusting wait this long after speech or playback, default 3.
    min_points: how many of your own settings before they shape the curve, default 2.
    """

    def __init__(self, config=None):
        super().__init__(PLUGIN_ID, 50, config or {})
        c = self.config
        self.speech_only = bool(c.get("speech_only", True))
        self.window = float(c.get("window_seconds", 90))
        self.step_seconds = float(c.get("step_seconds", 5))
        self.max_step = float(c.get("max_step", 0.02))
        self.deadband = float(c.get("deadband", 0.03))
        self.hold_seconds = float(c.get("hold_seconds", 300))
        self.tail_seconds = float(c.get("tail_seconds", 3))
        self.warmup_seconds = min(30.0, self.window / 3)
        self.curve = Curve(low=float(c.get("low", 0.35)), high=float(c.get("high", 0.75)),
                           min_points=int(c.get("min_points", 2)))
        self.room = RoomRange()
        self.enabled = bool(c.get("enabled", True))
        self.path = Path(c["state_file"]) if c.get("state_file") else state_path()

        self._lock = threading.RLock()
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=100)
        self._power: Optional[float] = None     # running mean square of the room audio
        self._measured = 0.0                    # seconds measured since start
        self._volume: Optional[float] = None    # the device's volume, as last heard
        self._muted = False
        self._speaking = self._recording = self._playing = False
        self._quiet_until = 0.0                 # monotonic: waiting out the tail of a pause
        self._hold_until = 0.0                  # monotonic: a manual change wins until then
        self._pending_manual = 0.0              # monotonic: a manual step whose result we wait for
        self._manual_volume: Optional[float] = None   # what the user last chose (media's volume)
        self._changed = False                   # we moved the volume since the user last chose
        self._room_saved = 0.0
        self._stop = threading.Event()
        self._threads = []
        self._load()

    # ---- setup --------------------------------------------------------------

    def bind(self, bus=None):
        super().bind(bus)
        on = self.bus.on
        on("ovos.autovolume.enable", self._on_enable)
        on("ovos.autovolume.disable", self._on_disable)
        on("ovos.autovolume.forget", self._on_forget)
        on("ovos.autovolume.status", self._on_status)
        on("recognizer_loop:audio_output_start", lambda m: self._pause("_speaking", True))
        on("recognizer_loop:audio_output_end", lambda m: self._pause("_speaking", False))
        on("recognizer_loop:record_begin", lambda m: self._pause("_recording", True))
        on("recognizer_loop:record_end", lambda m: self._pause("_recording", False))
        on("ovos.common_play.player.state", self._on_player_state)
        on("mycroft.volume.set", self._on_volume_set)
        for kind in ("increase", "decrease", "unmute", "mute.toggle"):
            on(f"mycroft.volume.{kind}", self._on_volume_step)
        on("mycroft.volume.get.response", self._on_volume_response)
        # the transformer service binds but never calls initialize(): start here
        if not self._threads:
            for target, name in ((self._measure_loop, "autovolume-measure"),
                                 (self._control_loop, "autovolume-control")):
                t = threading.Thread(target=target, name=name, daemon=True)
                t.start()
                self._threads.append(t)
        self.bus.emit(Message("mycroft.volume.get", {}, {"source": PLUGIN_ID}))
        LOG.info(f"auto volume {'on' if self.enabled else 'off'} "
                 f"({len(self.curve.points)} learned points, speech_only={self.speech_only})")

    def default_shutdown(self):
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass                    # daemon threads, they end with the listener
        self._save()

    # ---- state on disk ------------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            LOG.warning(f"auto volume: could not read {self.path}, starting fresh")
            return
        self.curve.points = Curve.points_from(data.get("points"))
        self.curve.points.sort(key=lambda p: p.noise_db)
        room = data.get("room") or {}
        self.room = RoomRange(counts=room.get("counts"), last=room.get("last"))
        if isinstance(data.get("enabled"), bool):
            self.enabled = data["enabled"]
        mv = data.get("manual_volume")
        self._manual_volume = float(mv) if isinstance(mv, (int, float)) else None

    def _save(self):
        with self._lock:
            data = {"enabled": self.enabled, "points": self.curve.to_list(),
                    "room": self.room.to_dict(), "manual_volume": self._manual_volume}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            LOG.exception(f"auto volume: could not save {self.path}")

    # ---- measuring ----------------------------------------------------------

    def on_audio(self, audio_data):
        """Room audio while waiting for the wake word. Never blocks the listener."""
        if self.enabled and not self.paused():
            try:
                self._queue.put_nowait(audio_data)
            except queue.Full:
                pass            # behind: drop it, never stall the microphone
        return audio_data

    def _measure_loop(self):
        while not self._stop.is_set():
            chunk = self._queue.get()
            if chunk is None:
                return
            try:
                self.measure(chunk)
            except Exception:
                LOG.exception("auto volume: could not measure a chunk")

    def measure(self, chunk: bytes):
        """Fold one chunk into the room's running level. The audio itself is not kept."""
        samples = np.frombuffer(chunk, dtype=np.int16)
        if not len(samples) or not self.enabled or self.paused():
            return
        x = samples.astype(np.float32) / 32768.0
        ms = float(np.mean(x * x))
        dt = len(samples) / float(self.sample_rate * max(1, self.channels))
        with self._lock:
            if self._power is None:
                self._power = ms
            else:
                a = 1.0 - math.exp(-dt / self.window)
                self._power += a * (ms - self._power)
            self._measured += dt

    def level_db(self) -> Optional[float]:
        with self._lock:
            if self._power is None:
                return None
            return max(SILENCE_DB, 10.0 * math.log10(self._power + 1e-12))

    def warm(self) -> bool:
        return self._measured >= self.warmup_seconds

    # ---- hands off ----------------------------------------------------------

    def paused(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return (self._speaking or self._recording or self._playing or self._muted
                or now < self._quiet_until)

    def _pause(self, flag: str, on: bool):
        was = getattr(self, flag)
        setattr(self, flag, on)
        if was and not on:
            self._quiet_until = time.monotonic() + self.tail_seconds
        if on:
            self._drain()

    def _drain(self):
        with self._queue.mutex:         # what was queued is the device's own sound now
            self._queue.queue.clear()

    def _on_player_state(self, message):
        playing = message.data.get("state") == OCP_PLAYING
        if playing == self._playing:
            return
        self._pause("_playing", playing)
        if playing and self.speech_only and self.enabled:
            self._restore_media_volume()

    def _restore_media_volume(self):
        """speech_only: media plays at the volume the user chose, not the room's speech volume."""
        if not self._changed or self._manual_volume is None or self._volume is None:
            return
        if abs(self._volume - self._manual_volume) > 0.005:
            self._set_volume(self._manual_volume)
        self._changed = False

    # ---- learning from the user ---------------------------------------------

    @staticmethod
    def _ours(message) -> bool:
        return (message.context or {}).get("source") == PLUGIN_ID

    def _on_volume_set(self, message):
        if self._ours(message):
            return
        try:
            percent = float(message.data["percent"])
        except (KeyError, TypeError, ValueError):
            return
        self._manual(clamp(percent, 0.0, 1.0))

    def _on_volume_step(self, message):
        """increase / decrease / unmute: the result comes in mycroft.volume.get.response."""
        if self._ours(message):
            return
        self._pending_manual = time.monotonic() + 3.0
        self._hold_until = time.monotonic() + self.hold_seconds
        t = threading.Timer(0.5, lambda: self.bus.emit(
            Message("mycroft.volume.get", {}, {"source": PLUGIN_ID})))
        t.daemon = True
        t.start()

    def _on_volume_response(self, message):
        try:
            percent = clamp(float(message.data["percent"]), 0.0, 1.0)
        except (KeyError, TypeError, ValueError):
            return
        self._volume = percent
        self._muted = bool(message.data.get("muted", False))
        if self._pending_manual and time.monotonic() < self._pending_manual:
            self._pending_manual = 0.0
            if not self._muted:
                self._manual(percent)

    def _manual(self, percent: float, now: Optional[float] = None, wall: Optional[float] = None):
        """The user chose ``percent``. It wins for a while, and it is learned."""
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        self._volume = percent
        self._hold_until = now + self.hold_seconds
        self._manual_volume = percent
        self._changed = False
        level = self.level_db()
        # only a choice for speech is learned: while something plays it is the media's volume,
        # and the level from before the device made sound is all there is
        if self._playing or level is None or not self.warm():
            self._save()
            return
        with self._lock:
            self.curve.learn(level, percent, wall)
        LOG.info(f"auto volume learned: {level:.1f} dBFS -> {percent:.2f} "
                 f"({len(self.curve.points)} points)")
        self._save()

    # ---- adjusting ----------------------------------------------------------

    def _control_loop(self):
        while not self._stop.wait(self.step_seconds):
            try:
                self.tick()
            except Exception:
                LOG.exception("auto volume: step failed")

    def target(self, wall: Optional[float] = None) -> Optional[float]:
        level = self.level_db()
        if level is None:
            return None
        with self._lock:
            return self.curve.volume(level, self.room.span(), time.time() if wall is None else wall)

    def tick(self, now: Optional[float] = None, wall: Optional[float] = None) -> Optional[float]:
        """One step: learn the room, and move the volume a little towards the curve.

        Returns the volume set, or None when nothing was changed.
        """
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        if not self.enabled or self.paused(now) or not self.warm():
            return None
        level = self.level_db()
        with self._lock:
            self.room.add(level, wall, weight=self.step_seconds)
        if now - self._room_saved > 600:
            self._room_saved = now
            self._save()
        if now < self._hold_until or self._volume is None:
            return None
        goal = self.target(wall)
        if goal is None:
            return None
        diff = goal - self._volume
        if abs(diff) < self.deadband:
            return None
        new = round(clamp(self._volume + clamp(diff, -self.max_step, self.max_step), 0.0, 1.0), 3)
        self._set_volume(new)
        self._changed = True
        return new

    def _set_volume(self, percent: float):
        self._volume = percent
        self.bus.emit(Message("mycroft.volume.set", {"percent": percent, "play_sound": False},
                              {"source": PLUGIN_ID}))

    # ---- bus commands -------------------------------------------------------

    def status(self) -> dict:
        level = self.level_db()
        span = self.room.span()
        goal = self.target()
        return {
            "enabled": self.enabled,
            "speech_only": self.speech_only,
            "points": len(self.curve.points),
            "learning": len(self.curve.points) >= self.curve.min_points,
            "level_db": None if level is None else round(level, 1),
            "quiet_db": None if span is None else round(span[0], 1),
            "noisy_db": None if span is None else round(span[1], 1),
            "target": None if goal is None else round(goal, 3),
            "volume": self._volume,
            "paused": self.paused(),
        }

    def _reply(self, message):
        self.bus.emit(message.response(self.status()))

    def _on_enable(self, message):
        self.enabled = True
        self._save()
        self._reply(message)

    def _on_disable(self, message):
        self.enabled = False
        self._drain()
        self._save()
        self._reply(message)

    def _on_forget(self, message):
        with self._lock:
            self.curve.forget()
            self.room.clear()
            self._manual_volume = None
        self._save()
        LOG.info("auto volume: forgot what it had learned")
        self._reply(message)

    def _on_status(self, message):
        self._reply(message)
