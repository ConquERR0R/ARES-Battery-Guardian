"""Real-hardware check of Smart Audio Override (Windows 11). Plays NO sound.

    .venv\\Scripts\\python.exe packaging\\audio_hw_check.py [--log packaging\\logs\\07_audio_hw_check.log]

Uses the REAL default playback device through Windows Core Audio and a temporary database (your
ARES data is not opened). For about two seconds the master volume goes to 100 % and is un-muted,
then the original volume and mute state are restored; the script checks each step and, whatever
happens, puts the original values back at the end. Scenarios: begin/end, "user changed it during
the alarm" (kept), and crash recovery (a second AudioOverride restores the record).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database.database import Database  # noqa: E402
from app.services import audio_override as ao  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=ROOT / "packaging" / "logs" / "07_audio_hw_check.log")
    args = ap.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    fails = 0

    def say(line: str) -> None:
        print(line, flush=True)
        out.append(line)

    def check(ok: bool, name: str, detail: str = "") -> None:
        nonlocal fails
        fails += 0 if ok else 1
        say(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))

    backend = ao.WindowsCoreAudio()

    def state(dev=None):
        ep = backend.open(dev)
        try:
            return ep.device_id, round(ep.get_volume(), 3), ep.get_mute()
        finally:
            ep.close()

    try:
        dev, vol0, mute0 = state()
    except ao.AudioUnavailable as exc:
        say(f"[SKIP] no default playback device: {exc}")
        args.log.write_text("\n".join(out) + "\n", encoding="utf-8")
        return 0
    say(f"Default playback device: {dev}")
    say(f"Original state: volume {vol0:.3f}, muted {mute0}")
    tmp = tempfile.TemporaryDirectory()
    db = Database(Path(tmp.name) / "audio.db")
    db.set_setting(ao.SETTING_KEY, True)
    try:
        # 1) begin / end
        ov = ao.AudioOverride(db, backend)
        say("begin: " + ov.begin("hardware check"))
        _, v, m = state(dev)
        already = vol0 >= 0.995 and not mute0
        check(already or (abs(v - 1.0) <= 0.02 and m is False), "override: 100% and not muted", f"{v:.3f}, muted {m}")
        time.sleep(0.5)
        say("end: " + ov.end("hardware check finished"))
        _, v, m = state(dev)
        check(abs(v - vol0) <= 0.01 and m == mute0, "restored exactly", f"{v:.3f}, muted {m}")
        # 2) user changes volume during the alarm -> kept
        if not already:
            ov.begin("hardware check (user change)")
            ep = backend.open(dev)
            try:
                ep.set_volume(0.42)          # what a user turning the volume down would do
            finally:
                ep.close()
            say("end: " + ov.end("hardware check finished"))
            _, v, m = state(dev)
            check(abs(v - 0.42) <= 0.02, "user's change during the alarm is kept", f"{v:.3f}")
            ep = backend.open(dev)
            try:
                ep.set_volume(vol0)
                ep.set_mute(mute0)
            finally:
                ep.close()
            # 3) crash recovery
            ao.AudioOverride(db, backend).begin("hardware check (crash)")   # object dropped = "crash"
            say("recover: " + ao.AudioOverride(db, backend).recover())
            _, v, m = state(dev)
            check(abs(v - vol0) <= 0.01 and m == mute0, "crash recovery restores", f"{v:.3f}, muted {m}")
        else:
            say("[SKIP] user-change and crash scenarios: the device is already at 100% and not muted")
    except Exception as exc:
        check(False, "unexpected error", f"{type(exc).__name__}: {exc}")
    finally:
        try:                                   # whatever happened: original values back
            ep = backend.open(dev)
            try:
                ep.set_volume(vol0)
                ep.set_mute(mute0)
            finally:
                ep.close()
        except Exception as exc:
            check(False, "final restore", str(exc))
        _, v, m = state(dev)
        check(abs(v - vol0) <= 0.01 and m == mute0, "final state equals the original", f"{v:.3f}, muted {m}")
        db.close()
        tmp.cleanup()
    say(f"Summary: {fails} failed")
    args.log.write_text("\n".join(out) + "\n", encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
