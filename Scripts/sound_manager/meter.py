# =============================================================================
# meter.py - Microphone level meter
# =============================================================================
# Records the selected microphone with `parec` at a very low sample rate
# (8 kHz mono, ~50 ms chunks) and reports the peak of each chunk as a value
# between 0 and 1 on a dB scale, so normal speech lands in the middle.
#
# The meter only runs while the Input page is visible, because recording
# keeps the microphone awake. Its stream uses METER_CLIENT_NAME, which the
# backend filters out of the Apps page.
# =============================================================================

from __future__ import annotations

import math
from array import array
from typing import Callable, Optional

from gi.repository import Gio, GLib

from .backend import METER_CLIENT_NAME

RATE = 8000
CHUNK_BYTES = RATE * 2 // 20      # 50 ms of 16-bit mono
FLOOR_DB = -60.0                  # shown as an empty bar
DECAY = 0.12                      # how fast the bar falls per chunk


class LevelMeter:
    def __init__(self, on_level: Callable[[float], None]) -> None:
        self.on_level = on_level
        self.source = ""
        self._process: Optional[Gio.Subprocess] = None
        self._cancel: Optional[Gio.Cancellable] = None
        self._level = 0.0

    def start(self, source: str) -> None:
        if source == self.source and self._process is not None:
            return
        self.stop()
        self.source = source
        try:
            self._process = Gio.Subprocess.new(
                ["parec", "-d", source, "--raw", "--format=s16le", f"--rate={RATE}",
                 "--channels=1", "--latency-msec=50",
                 f"--client-name={METER_CLIENT_NAME}", "--stream-name=Level meter"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            self._process = None
            return
        self._cancel = Gio.Cancellable()
        self._read(self._process.get_stdout_pipe(), self._cancel)

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
            self._cancel = None
        if self._process is not None:
            self._process.force_exit()
            self._process = None
        self.source = ""
        self._level = 0.0
        self.on_level(0.0)

    def _read(self, stream: Gio.InputStream, cancel: Gio.Cancellable) -> None:
        stream.read_bytes_async(CHUNK_BYTES, GLib.PRIORITY_DEFAULT, cancel,
                                self._on_chunk, cancel)

    def _on_chunk(self, stream, result, cancel) -> None:
        try:
            data = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            return
        if cancel.is_cancelled():
            return
        if not data:
            # parec exited (device removed); the next refresh restarts it.
            self._process = None
            self.source = ""
            self.on_level(0.0)
            return
        samples = array("h")
        samples.frombytes(data[: len(data) - len(data) % 2])
        peak = max((abs(s) for s in samples), default=0) / 32768
        db = 20 * math.log10(peak) if peak > 0 else FLOOR_DB
        level = max(0.0, min(1.0, 1 - db / FLOOR_DB))
        self._level = level if level > self._level else max(level, self._level - DECAY)
        self.on_level(self._level)
        self._read(stream, cancel)
