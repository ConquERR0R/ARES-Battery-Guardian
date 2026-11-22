"""Smart Audio Override: make a critical protection alarm audible, then put the sound back.

Optional, OFF by default (Settings → D. "Smart Audio Override"). When a High or Low Battery
Protection alarm starts, ARES:

  1. reads the DEFAULT PLAYBACK device's master volume and mute state (Windows Core Audio,
     IAudioEndpointVolume), and saves them in the database BEFORE changing anything;
  2. un-mutes the device and sets its master volume to 100 %;
  3. plays the alarm (the existing sound_service / AlarmPlayer, unchanged).

When the alarm stops (protection released, emergency release, mode change, or ARES exits normally)
ARES restores the saved volume and mute state on that same device.

If the user changes the volume or mute DURING the alarm (expected behaviour, see
docs/V2_SMART_AUDIO_OVERRIDE.md):
  * ARES never overrides again during that protection session (repeat alarms play at whatever
    level the user chose);
  * at the end ARES does NOT restore the old values, because the user's own latest choice wins.
    "Changed" means the device no longer shows what ARES set (100 %, un-muted).

Crash / power-loss recovery (best effort): the saved state stays in the database until it has been
restored. On the next start ARES restores it if the device still shows what ARES set, otherwise it
leaves the user's current setting alone. Windows keeps a device's volume across reboots, so this
also covers a reboot while the override was active.

Only the system-wide master volume and mute of one device are touched; per-application volumes,
other devices and Windows sound settings are never changed. Every failure (no audio device, COM
error, device unplugged) is logged and the alarm still plays; nothing here ever raises.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger(__name__)

SETTING_KEY = "audio_override_enabled"
STATE_KEY = "audio_override"            # pending restore record (controller_state table)
TARGET_VOLUME = 1.0
TOLERANCE = 0.02                         # scalar volume compare tolerance (2 %)
MAX_RECOVERY_ATTEMPTS = 5


class AudioUnavailable(Exception):
    """No default playback device, or the Windows audio API failed."""


class Endpoint(Protocol):
    device_id: str
    name: str

    def get_volume(self) -> float: ...
    def set_volume(self, value: float) -> None: ...
    def get_mute(self) -> bool: ...
    def set_mute(self, muted: bool) -> None: ...
    def close(self) -> None: ...


class AudioBackend(Protocol):
    def open(self, device_id: str | None = None) -> Endpoint:
        """The default playback device (device_id None) or a specific one. Raises AudioUnavailable."""
        ...


# --------------------------------------------------------------------------------------------
# Windows Core Audio (ctypes COM; no extra dependency)
# --------------------------------------------------------------------------------------------
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850         # 0x80010106: COM already initialised as MTA - fine to use
E_RENDER, E_CONSOLE = 0, 0
# vtable slots (IUnknown = 0..2)
_ENUM_GET_DEFAULT, _ENUM_GET_DEVICE = 4, 5
_DEV_ACTIVATE, _DEV_GET_ID = 3, 5
_VOL_SET_SCALAR, _VOL_GET_SCALAR, _VOL_SET_MUTE, _VOL_GET_MUTE = 7, 9, 14, 15


def _guid(text: str):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _call(obj, index: int, *args, argtypes=()):
    """Call COM method `index` of interface pointer `obj` (an int). HRESULT errors raise OSError."""
    import ctypes

    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_ptr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return proto(fn_ptr)(obj, *args)


def _release(obj) -> None:
    import ctypes

    if obj:
        try:
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
            fn_ptr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[2]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(fn_ptr)(obj)
        except Exception:                                   # pragma: no cover - Windows only
            pass


def _co_uninitialize() -> None:
    import ctypes

    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:                                       # pragma: no cover - Windows only
        pass


class _WindowsEndpoint:
    def __init__(self, volume_ptr: int, device_id: str, com_initialised: bool) -> None:
        self._vol = volume_ptr
        self._com = com_initialised          # this open() call added a COM reference: undo on close
        self.device_id = device_id
        self.name = device_id

    def get_volume(self) -> float:
        import ctypes

        v = ctypes.c_float()
        _call(self._vol, _VOL_GET_SCALAR, ctypes.byref(v), argtypes=(ctypes.POINTER(ctypes.c_float),))
        return float(v.value)

    def set_volume(self, value: float) -> None:
        import ctypes

        _call(self._vol, _VOL_SET_SCALAR, ctypes.c_float(max(0.0, min(1.0, value))), None,
              argtypes=(ctypes.c_float, ctypes.c_void_p))

    def get_mute(self) -> bool:
        import ctypes

        m = ctypes.c_int()
        _call(self._vol, _VOL_GET_MUTE, ctypes.byref(m), argtypes=(ctypes.POINTER(ctypes.c_int),))
        return bool(m.value)

    def set_mute(self, muted: bool) -> None:
        import ctypes

        _call(self._vol, _VOL_SET_MUTE, 1 if muted else 0, None, argtypes=(ctypes.c_int, ctypes.c_void_p))

    def close(self) -> None:
        _release(self._vol)
        self._vol = 0
        if self._com:
            self._com = False
            _co_uninitialize()


class WindowsCoreAudio:
    """IMMDeviceEnumerator → IMMDevice → IAudioEndpointVolume, opened fresh for every operation
    (so a changed default device is always seen) and released straight afterwards."""

    def open(self, device_id: str | None = None) -> Endpoint:
        if sys.platform != "win32":
            raise AudioUnavailable("Windows audio is only available on Windows.")
        import ctypes

        ole32 = ctypes.OleDLL("ole32")
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            raise AudioUnavailable(f"COM initialisation failed (0x{hr & 0xFFFFFFFF:08X})")
        ep = None
        enum = ctypes.c_void_p()
        dev = ctypes.c_void_p()
        vol = ctypes.c_void_p()
        try:
            clsid, iid_enum, iid_vol = (_guid(CLSID_MMDeviceEnumerator), _guid(IID_IMMDeviceEnumerator),
                                        _guid(IID_IAudioEndpointVolume))
            ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid_enum), ctypes.byref(enum))
            if device_id:
                _call(enum.value, _ENUM_GET_DEVICE, device_id, ctypes.byref(dev),
                      argtypes=(ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)))
            else:
                _call(enum.value, _ENUM_GET_DEFAULT, E_RENDER, E_CONSOLE, ctypes.byref(dev),
                      argtypes=(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))
            pid = ctypes.c_wchar_p()
            _call(dev.value, _DEV_GET_ID, ctypes.byref(pid), argtypes=(ctypes.POINTER(ctypes.c_wchar_p),))
            ident = pid.value or ""
            ctypes.windll.ole32.CoTaskMemFree(pid)
            _call(dev.value, _DEV_ACTIVATE, ctypes.byref(iid_vol), CLSCTX_ALL, None, ctypes.byref(vol),
                  argtypes=(ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)))
            ep = _WindowsEndpoint(vol.value, ident, com_initialised=hr >= 0)
            vol = ctypes.c_void_p()                 # now owned by the endpoint
            return ep
        except OSError as exc:
            what = "the saved audio device is not available" if device_id else "no default playback device"
            raise AudioUnavailable(f"{what} ({exc})") from exc
        finally:
            _release(vol.value)
            _release(dev.value)
            _release(enum.value)
            if ep is None and hr >= 0:              # failed: undo this call's COM reference
                _co_uninitialize()


class NoAudio:
    def open(self, device_id: str | None = None) -> Endpoint:
        raise AudioUnavailable("Windows audio is only available on Windows.")


def default_backend() -> AudioBackend:
    return WindowsCoreAudio() if sys.platform == "win32" else NoAudio()


# --------------------------------------------------------------------------------------------
# Override lifecycle
# --------------------------------------------------------------------------------------------
@dataclass
class Saved:
    device_id: str
    volume: float
    mute: bool
    started: float
    reason: str = ""
    attempts: int = 0

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "volume": self.volume, "mute": self.mute,
                "started": self.started, "reason": self.reason, "attempts": self.attempts}

    @classmethod
    def from_dict(cls, d: dict) -> "Saved":
        return cls(str(d["device_id"]), float(d["volume"]), bool(d["mute"]), float(d.get("started", 0)),
                   str(d.get("reason", "")), int(d.get("attempts", 0)))


def _pct(v: float) -> str:
    return f"{round(v * 100)}%"


class AudioOverride:
    """Used by the ProtectionManager on the GUI thread: begin() before the first alarm of a
    protection session, end() when the alarm stops, recover() once at start-up."""

    def __init__(self, db, backend: AudioBackend | None = None, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.backend = backend or default_backend()
        self.clock = clock
        self.saved: Saved | None = None          # override active in THIS process
        self.last_message = ""

    # -- helpers ------------------------------------------------------------------------------
    def enabled(self) -> bool:
        try:
            return bool(self.db.get_setting(SETTING_KEY))
        except Exception:
            return False

    @property
    def active(self) -> bool:
        return self.saved is not None

    def _event(self, kind: str, msg: str) -> None:
        self.last_message = msg
        log.info("Audio override: %s", msg)
        try:
            self.db.log_event(kind, f"Smart Audio Override: {msg}")
        except Exception:
            log.exception("Could not record audio override event")

    @staticmethod
    def _still_overridden(ep: Endpoint) -> bool:
        return abs(ep.get_volume() - TARGET_VOLUME) <= TOLERANCE and not ep.get_mute()

    # -- lifecycle ----------------------------------------------------------------------------
    def begin(self, reason: str) -> str:
        """Called just before a protection alarm starts. Returns a short description ('' if off).
        Never overrides twice in one session and never raises."""
        if self.saved is not None or not self.enabled():
            return ""
        ep = None
        try:
            if self.db.get_state(STATE_KEY):     # an older override was never restored: do that first
                self.recover()
            ep = self.backend.open()
            vol, mute = ep.get_volume(), ep.get_mute()
            if vol >= TARGET_VOLUME - TOLERANCE / 4 and not mute:
                self._event("info", f"volume already {_pct(vol)} and not muted; nothing changed ({reason}).")
                return self.last_message
            saved = Saved(ep.device_id, vol, mute, self.clock(), reason)
            self.db.set_state(STATE_KEY, saved.as_dict())   # saved BEFORE anything changes
            self.saved = saved
            try:
                ep.set_volume(TARGET_VOLUME)
                if mute:
                    ep.set_mute(False)
            except Exception as exc:                        # put back whatever was changed
                log.warning("Audio override could not be applied: %s", exc)
                self._restore_on(ep, saved, force=True)
                self.saved = None
                self.db.set_state(STATE_KEY, None)
                self._event("warning", f"could not change the Windows volume ({exc}); the alarm plays "
                                       "at the current volume.")
                return self.last_message
            self._event("info", f"{'un-muted, ' if mute else ''}volume {_pct(vol)} → 100% for the {reason}.")
            return self.last_message
        except AudioUnavailable as exc:
            self._event("warning", f"not applied: {exc}. The alarm plays at the current volume.")
            return self.last_message
        except Exception as exc:
            log.exception("Audio override failed")
            self._event("warning", f"not applied ({exc}). The alarm plays at the current volume.")
            return self.last_message
        finally:
            if ep is not None:
                ep.close()

    def end(self, reason: str) -> str:
        """Called when the alarm stops (release, emergency release, exit). Never raises."""
        saved, self.saved = self.saved, None
        if saved is None:
            return ""
        return self._finish(saved, reason)

    def recover(self) -> str:
        """Start-up: restore an override left behind by a crash / power loss (best effort)."""
        try:
            raw = self.db.get_state(STATE_KEY)
        except Exception:
            return ""
        if not raw or self.saved is not None:
            return ""
        try:
            saved = Saved.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            self.db.set_state(STATE_KEY, None)
            return ""
        return self._finish(saved, "recovery after ARES did not exit normally")

    def _finish(self, saved: Saved, reason: str) -> str:
        ep = None
        try:
            ep = self.backend.open(saved.device_id)
            if not self._still_overridden(ep):
                self.db.set_state(STATE_KEY, None)
                self._event("info", f"you changed the volume or mute while the alarm was active; your "
                                    f"setting is kept (not restored to {_pct(saved.volume)}"
                                    f"{', muted' if saved.mute else ''}). ({reason})")
                return self.last_message
            self._restore_on(ep, saved)
            self.db.set_state(STATE_KEY, None)
            self._event("info", f"restored volume {_pct(saved.volume)}{' and mute' if saved.mute else ''} ({reason}).")
            return self.last_message
        except Exception as exc:
            saved.attempts += 1
            if saved.attempts >= MAX_RECOVERY_ATTEMPTS:
                self.db.set_state(STATE_KEY, None)
                self._event("warning", f"could not restore the volume after {saved.attempts} attempts ({exc}); "
                                       f"it was {_pct(saved.volume)}{', muted' if saved.mute else ''}. "
                                       "Please set it yourself.")
            else:
                self.db.set_state(STATE_KEY, saved.as_dict())   # retried at the next start / alarm
                self._event("warning", f"could not restore the volume yet ({exc}); ARES tries again at "
                                       "its next start.")
            return self.last_message
        finally:
            if ep is not None:
                ep.close()

    @staticmethod
    def _restore_on(ep: Endpoint, saved: Saved, force: bool = False) -> None:
        errors = []
        for step in ((lambda: ep.set_volume(saved.volume)), (lambda: ep.set_mute(saved.mute))):
            try:
                step()
            except Exception as exc:
                errors.append(exc)
        if errors and not force:
            raise errors[0]
