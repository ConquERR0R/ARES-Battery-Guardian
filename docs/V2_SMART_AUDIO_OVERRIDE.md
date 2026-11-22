# Smart Audio Override (V2 Beta)

This feature makes a High or Low Battery Protection alarm audible even if Windows is muted or
turned down. When the alarm stops, it puts your volume and mute setting back.

It is **optional and off by default**. Turn it on in **Settings → D. Monitoring & notifications →
Smart Audio Override**, then click **Save**.

## What happens

| When | What ARES does |
|---|---|
| A protection screen appears and its alarm will sound | Reads the default playback device's master volume and mute state, and saves both in the database. Then sets the volume to 100% and un-mutes the device, and only after that plays the first alarm sound. |
| The volume is already 100% and not muted | Nothing is changed or saved. |
| Repeat alarms (every N minutes) | No new override. The repeat plays at whatever level the device has now. |
| The alarm stops: charger unplugged or connected, emergency release (Ctrl+Alt+Shift+U), the screen changes mode, or you choose **Exit ARES** | Puts back the saved volume and mute state on **the same device**. If you plugged in headphones meanwhile, they are not touched. |
| ARES is killed, crashes or the power fails during an alarm | The saved state stays in the database. At the next start ARES restores it. This also works after a reboot, because Windows keeps each device's volume. |

## If you change the volume yourself during an alarm

"Changed" means the device no longer shows what ARES set (100%, not muted), allowing for 2%
rounding. If that happens:

1. ARES never overrides the volume again in that protection session.
2. When the alarm stops, ARES does **not** restore the old values. Your latest choice is kept, and
   the event log says so.

The same rule applies to crash recovery. If you changed the volume after ARES stopped, your
setting is kept.

## Safety

- **What is touched:** only the default playback device's master volume and mute. Windows Core
  Audio `IAudioEndpointVolume` is called through `ctypes`, with no extra package. Per-app volumes,
  other devices, the Windows sound scheme and sound settings are never changed.
- **The change is temporary:** nothing persists after the restore. Other apps get louder only while
  the override is active, because it is a system-wide master volume.
- **Failures are safe:** if there is no audio device, COM fails or access is denied, ARES logs a
  warning and the alarm still plays at the current volume. If setting the volume fails partway,
  ARES puts back what it changed.
- **Unplugged devices:** if the device was unplugged before the restore, ARES retries at the next
  start. After 5 failed attempts it stops and the event log asks you to set the volume yourself.
- **Your settings and history:** no existing battery setting or history changes. The feature adds
  one setting, `audio_override_enabled`, and one state record, `audio_override`.

## Tests

- `tests/test_audio_override.py` has 28 tests using mocked Windows audio. It covers:
  - off by default
  - exact restore of volume and mute
  - an already-audible device
  - no second override
  - user changes during an alarm
  - no device, API failure, and partial rollback
  - default device changed during an alarm
  - device unplugged, with retry and give-up
  - crash recovery
  - a corrupt record
  - the ProtectionManager lifecycle: order before the first sound, unplug, repeat, low mode, a silent alarm, exit, and recovery at start-up
- `packaging/audio_hw_check.py` is the real-hardware check, run by `packaging\build_installer.bat test`.
  - It uses the real default device and plays **no sound**.
  - For about 2 seconds the volume is 100% and un-muted. It is then restored and checked.
  - It also checks the "user change is kept" and crash-recovery cases on real hardware.
  - Whatever happens, it puts the original values back at the end.

## Limitations

- Only the **default playback device for system sounds** (Console role) is used. If Windows sends
  ARES's sound to another device, that device is not changed.
- ARES's own **per-app volume** in the Windows Volume Mixer is not changed. If you muted ARES there,
  the alarm stays silent.
- **Focus Assist / Do Not Disturb**, Bluetooth headsets that ignore the master volume, and hardware
  mute keys that bypass Windows are outside ARES's control.
- While the override is active, every app on that device is louder, because the master volume is
  system-wide.
- **Crash recovery is best effort.** It runs at the next start of ARES, not at the moment of the
  crash.
- **Hardware testing:** "Compatible" is claimed only for hardware where `audio_hw_check.py` has
  passed. Results are recorded in `docs/V2_BETA_INSTALLER.md`.
