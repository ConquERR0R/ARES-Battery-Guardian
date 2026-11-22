"""Audible alerts using Python's built-in winsound module (Windows only, no extra dependency).

Sounds are played asynchronously (SND_ASYNC) so no thread ever waits for playback.

Two protection alarms have different default sounds:
  * high battery (charger connected at the limit): Windows "Exclamation" sound
  * low battery (charger disconnected at the low limit): Windows "Battery Low" sound
A custom .wav file can replace either. A missing or invalid custom file is never replaced
silently: the default plays (an alarm must still be heard) and the result says so, so it is
logged and shown in Settings. The saved path is kept as it is.

Volume: winsound has no volume control, so for volumes below 100% ARES plays a scaled COPY of
the WAV from its cache folder (8/16-bit PCM). The user's own file is only ever read.
"""
from __future__ import annotations

import array
import hashlib
import logging
import os
import sys
import wave
from pathlib import Path

log = logging.getLogger(__name__)

HIGH = "high"
LOW = "low"

# Windows sound-scheme aliases (Control Panel > Sound > Sounds). "SystemExclamation" is
# audible on a default Windows 11 install; "SystemHand" is the "Critical Stop" sound.
DEFAULT_ALIAS = "SystemExclamation"
_MEDIA = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Media"
DEFAULTS = {
    # (preferred file shipped with Windows, fallback alias)
    HIGH: (_MEDIA / "Windows Exclamation.wav", "SystemExclamation"),
    LOW: (_MEDIA / "Windows Battery Low.wav", "SystemHand"),
}


def default_description(kind: str) -> str:
    f, alias = DEFAULTS.get(kind, (None, DEFAULT_ALIAS))
    return f"Windows default ({f.name})" if f is not None else f"Windows default ({alias})"


def check_wav(path: str | None) -> str | None:
    """None if `path` is empty or a usable .wav file; otherwise a short problem description."""
    if not path:
        return None
    p = Path(path)
    if p.suffix.lower() != ".wav":
        return "Not a .wav file; the default sound will be used."
    if not p.is_file():
        return "File not found; the default sound will be used."
    try:
        with p.open("rb") as fh:
            head = fh.read(12)
    except OSError as exc:
        return f"Cannot read the file ({exc}); the default sound will be used."
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return "Not a valid WAV (RIFF/WAVE) file; the default sound will be used."
    return None


def _winsound():
    if sys.platform != "win32":
        return None
    try:
        import winsound
    except ImportError:
        return None
    return winsound


def scaled_copy(path: str | Path, volume: int, cache: Path | None = None) -> tuple[Path, str]:
    """Return (file to play, note). For volume < 100 a scaled copy in the cache folder; the source
    is never modified. Falls back to the original file (with a note) if it can't be scaled."""
    src = Path(path)
    volume = max(0, min(100, int(volume)))
    if volume >= 100:
        return src, ""
    try:
        st = src.stat()
        if cache is None:
            from app import paths
            cache = paths.cache_dir() / "sounds"
        cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{src.resolve()}|{st.st_mtime_ns}|{st.st_size}|{volume}".encode()).hexdigest()[:16]
        out = cache / f"{key}.wav"
        if out.is_file():
            return out, ""
        with wave.open(str(src), "rb") as w:
            params = w.getparams()
            frames = w.readframes(params.nframes)
        f = volume / 100.0
        if params.sampwidth == 2:
            a = array.array("h")
            a.frombytes(frames[: len(frames) // 2 * 2])
            if sys.byteorder != "little":
                a.byteswap()
            a = array.array("h", (int(x * f) for x in a))
            if sys.byteorder != "little":
                a.byteswap()
            data = a.tobytes()
        elif params.sampwidth == 1:                     # unsigned 8-bit, centre 128
            data = bytes(int(128 + (b - 128) * f) for b in frames)
        else:
            return src, f"volume not applied ({params.sampwidth * 8}-bit WAV)"
        tmp = out.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as w:
            w.setparams(params)
            w.writeframes(data)
        os.replace(tmp, out)
        return out, ""
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        return src, f"volume not applied ({exc})"


def play_alarm(kind: str, wav_path: str | None = None, volume: int = 100) -> tuple[bool, str]:
    """Play the alarm for `kind` (HIGH/LOW): the custom .wav if usable, else the Windows default.

    Returns (played, description). A description starting with "⚠" means the custom file could
    not be used. Never raises.
    """
    ws = _winsound()
    if ws is None:
        return False, "Sound is only available on Windows."
    if int(volume) <= 0:
        return False, "Alarm volume is 0% (muted)."
    warn = ""
    try:
        if wav_path:
            problem = check_wav(wav_path)
            if problem is None:
                f, note = scaled_copy(wav_path, volume)
                ws.PlaySound(str(f), ws.SND_FILENAME | ws.SND_ASYNC | ws.SND_NODEFAULT)
                return True, f"Played {Path(wav_path).name}" + (f" ({note})" if note else "")
            log.warning("Alarm sound %s not usable: %s", wav_path, problem)
            warn = f"⚠ Custom sound {Path(wav_path).name} not usable ({problem.split(';')[0]}); "
        f, alias = DEFAULTS.get(kind, (None, DEFAULT_ALIAS))
        if f is not None and f.is_file():
            g, note = scaled_copy(f, volume)
            ws.PlaySound(str(g), ws.SND_FILENAME | ws.SND_ASYNC | ws.SND_NODEFAULT)
            return True, warn + f"played {f.name}" if warn else f"Played {f.name}" + (f" ({note})" if note else "")
        ws.PlaySound(alias, ws.SND_ALIAS | ws.SND_ASYNC)
        return True, warn + f"played the Windows {alias} sound" if warn else f"Played the Windows {alias} sound"
    except RuntimeError as exc:          # winsound raises RuntimeError if the sound can't be played
        log.warning("Could not play alarm sound: %s", exc)
        try:
            ws.MessageBeep(ws.MB_ICONEXCLAMATION)
            return True, "Played a system beep (alarm sound unavailable)"
        except RuntimeError:
            return False, f"Could not play sound: {exc}"


def play_alert(wav_path: str | None = None) -> tuple[bool, str]:
    """Generic alert (used for the maximum-charge notification when protection is off)."""
    return play_alarm(HIGH, wav_path)


def stop() -> None:
    """Stop any sound ARES is playing asynchronously. Never raises."""
    ws = _winsound()
    if ws is None:
        return
    try:
        ws.PlaySound(None, 0)
    except RuntimeError:
        pass


class AlarmPlayer:
    """Default player used by the protection manager (tests inject a fake)."""

    def play(self, kind: str, wav_path: str | None, volume: int = 100) -> tuple[bool, str]:
        return play_alarm(kind, wav_path, volume)

    def stop(self) -> None:
        stop()
