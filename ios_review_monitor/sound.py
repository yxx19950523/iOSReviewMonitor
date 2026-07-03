from __future__ import annotations

import math
import platform
import struct
import subprocess
import tempfile
import wave
from pathlib import Path


def _tone_file(frequencies: list[int], duration: float = 0.18) -> Path:
    sample_rate = 44100
    gap = int(sample_rate * 0.05)
    fd, name = tempfile.mkstemp(prefix="ios_review_monitor_", suffix=".wav")
    path = Path(name)
    with wave.open(name, "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for frequency in frequencies:
            frames = int(sample_rate * duration)
            for i in range(frames):
                value = int(22000 * math.sin(2 * math.pi * frequency * (i / sample_rate)))
                wav.writeframesraw(struct.pack("<h", value))
            wav.writeframesraw(b"\x00\x00" * gap)
    try:
        import os

        os.close(fd)
    except OSError:
        pass
    return path


def play_sound(kind: str) -> None:
    system = platform.system()
    frequencies = [880, 1175] if kind == "in_review" else [660, 880, 1320]

    if system == "Windows":
        try:
            import winsound

            for frequency in frequencies:
                winsound.Beep(frequency, 180)
            return
        except Exception:
            pass

    wav_path = _tone_file(frequencies)
    try:
        if system == "Darwin":
            subprocess.Popen(["afplay", str(wav_path)])
        elif system == "Linux":
            subprocess.Popen(["paplay", str(wav_path)])
        else:
            print("\a", end="", flush=True)
    except Exception:
        print("\a", end="", flush=True)
