# ARES Battery Guardian V2: Smart Wake-Up Protection and persistent settings

Status: implemented and tested **in a Linux build environment with simulated Windows parts**
(182 tests pass, 0 skipped, with Python 3.14.3 and the exact versions in `requirements.txt`). Nothing in this document has been run on Windows 11 or on a Dell
Latitude yet. Waking from Sleep is **not confirmed on any hardware** until the manual tests in
section 5 have been done on that laptop.

---

## 1. Smart Wake-Up Protection

### What it does
While the laptop sleeps, ARES cannot run. Smart Wake-Up asks **Windows Task Scheduler** to wake the
PC every few minutes, read the battery, and react:

| Condition after the wake | What happens |
|---|---|
| on battery and ≤ **25%** (low threshold) | ARES is activated (or started, single instance) and shows the existing **Low Battery Protection** screen and alarm |
| on charger and ≥ **89%** (high threshold) | ARES is activated and keeps the PC awake (screen may stay off) until the battery reaches the **90%** protection threshold, then the existing **High Battery Protection** screen and alarm appear. The 90% threshold is unchanged. |
| anything else | the result is recorded and the check process exits; Windows returns the PC to Sleep by itself |

ARES **never forces Sleep**. After an unattended timer wake, Windows goes back to Sleep after its own
idle timeout, and stays awake if you start using the PC.

### How it works (supported Windows mechanisms only)
- One per-user Task Scheduler task, **"ARES Battery Guardian Wake Check"**, with *Wake the computer to
  run this task*. It runs with your normal rights (no administrator, no driver, no keyboard hook, no
  change to any Windows or power setting). Its action is `ARES-Battery-Guardian.exe --wake-check`
  (from source: `pythonw.exe main.py --wake-check`).
- Two triggers: the **next check** (rescheduled by every check so the interval can adapt) and an
  **hourly backstop**, so the chain can't stop silently. *Run as soon as possible after a missed
  start* is on; a late check is reported as "ran N min late".
- `--wake-check` is a small process: it reads `GetSystemPowerStatus` (percent and charger) through
  `app/core/power_probe.py`, which has no Qt dependency, decides,
  and, only if a threshold is reached, sends `wake:low` / `wake:high` to the running ARES over the
  single-instance socket, or starts ARES in the tray if it isn't running. No window, no tray icon.
  It loads only QtCore and QtNetwork (for that socket); QtGui, QtWidgets and the `app.ui` code are
  never imported, which a test enforces by making those imports fail inside a real
  `--wake-check` run.
- Keep-awake uses the documented `SetThreadExecutionState` API from the running ARES, and only:
  (a) while a protection screen is visible, so it is seen and heard, and
  (b) after an 89% wake on the charger, until 90% (max 60 min, ends at once when unplugged).

### Safeguards
- **No duplicates:** the task is created with `/F` (replaces the one task of that name);
  *Do not start a new instance* plus a lock file prevent overlapping checks.
- **Wake frequency cap:** never more often than every 5 min; more than 12 checks in an hour slows to
  hourly.
- **Battery drain:** the interval grows when the battery is far from a threshold (×3, ×6, up to
  60 min). On battery below 5% only the hourly backstop remains. A protection screen on battery
  keeps the display on for at most 15 min, after which Windows' own sleep rules apply again (the
  screen itself stays).
- **Missing telemetry:** an unreadable battery or charger never triggers anything; the next check is
  scheduled normally.
- **Turn it off immediately:** tray menu → *Smart Wake-Up* (untick), or Settings → G → untick → Save,
  or `ARES-Battery-Guardian.exe --disable-wake` (works even if ARES isn't running). Each removes the
  task at once.
- It is **off by default**.

### Limitations (also shown in Settings → G)
- **No wake from Shut down.** Nothing can run while the PC is off.
- **Hibernate:** waking from Hibernate with a wake timer is not guaranteed.
- **Wake timers may be disabled** in the power plan (*Allow wake timers*), often on battery. ARES
  shows the current AC/DC values and never changes them.
- **Modern Standby** PCs: Windows decides when scheduled tasks run during standby.
- The task runs only while you are **signed in** (Sleep keeps you signed in; after a restart, checks
  start again once you sign in).
- Every wake **uses battery**. Windows may **delay or skip** wakes.
- ARES does not control charging. The screens are reminders; real charge limiting is only the Dell
  firmware threshold (section A).

### Settings section G
Enable · Low-battery wake (on/off, threshold, default 25%) · High-battery wake (on/off, threshold,
default 89%) · Check interval (5–60 min, default 5) · Wake-timer support (from `GetPwrCapabilities`
and the power plan's *Allow wake timers*, with a `powercfg` fallback) · Sleep state · Last wake check
result · **Test wake configuration** (checks the saved settings, the registered task, its wake flag
and command, and the PC's support; it does **not** put the PC to sleep) · Re-check support.

---

## 2. Persistent settings

### Where settings are stored
One SQLite database, the same one that holds the history, in a per-user folder:

`%LOCALAPPDATA%\ARES Battery Guardian\data\battery_guardian.db`

- The same folder for the EXE and for a copy run from source. It is never the program folder, the
  PyInstaller extraction folder or a temp folder, so updates, rebuilds and reinstalls keep it.
- LOCALAPPDATA (not the roaming APPDATA) because the history grows and must not sync between PCs,
  and because packaged ARES builds already used this folder. Keeping it out of OneDrive also avoids
  sync conflicts on an open SQLite file (your project folder is inside OneDrive).
- `ARES_DATA_DIR` overrides the folder (tests and portable copies). Such a run never changes Windows startup or the Smart Wake-Up task: both belong to the normal data folder.

### Migration strategy
- **Old source-folder data:** on first start, a database found in `<project>\data\` is copied once
  into the new folder with SQLite's online backup API (atomic rename). Only when the new database
  doesn't exist yet; the old file is left untouched. The copy is logged in Events.
- **Schema upgrades:** the schema version is kept in `PRAGMA user_version` (now 2). Before upgrading,
  ARES writes a full backup to `data\backups\` (newest 5 kept), then upgrades inside one transaction.
  New settings get defaults in code and are **not** written, so nothing you saved is ever replaced.
  Legacy keys from v1.1 are mapped once (`settings_version`).
- **Corruption:** if the file fails `PRAGMA quick_check` or can't be opened, it is renamed to
  `battery_guardian.corrupt-<date>.db` (with its -wal/-shm), saved settings are salvaged where
  readable, a new database is started and an Events entry explains what happened.

### Saving
- Every control on the Settings page is staged. **Save settings** validates all values (firmware
  pair, low < high protection threshold, wake low < wake high, interval 5–60, theme), then writes all
  changes in **one transaction** and **reads them back**. Only then does the status line show
  *✓ All changes saved HH:MM:SS*. Otherwise *Save failed: …* (nothing is written) or
  *● Unsaved changes*.
- After a successful save each changed setting is pushed to the service that uses it: protection
  screens and alarms, monitoring interval, firmware auto-apply, Windows startup registration, the
  wake task (one update per save) and the theme.
- **Discard changes** shows the stored values again. Closing the dashboard with unsaved changes
  asks *Save / Discard / Cancel*; **Exit ARES** asks *Save / Discard*.
- SQLite WAL with committed transactions: a saved value survives a crash, a forced Windows restart
  or a power loss right after Save (tested by ending the process without closing the database).
- **Restore default settings…** asks first and then deletes saved preferences only. Battery history,
  sessions, health data and events are kept; the Windows startup entry is re-applied to the default.

### What is persisted
Thresholds and their switches · per-alarm on/off, sound file, volume, repeat · master sound switch ·
monitoring and notification settings · start with Windows, start minimized · Smart Wake-Up settings ·
theme (dark/light), window size and position, last tab · preferred power profile and
*apply on startup* (off by default; the active Windows plan is shown, and the profile is only applied
when you click *Apply profile now…* or enable that option) · cctk path. The BIOS password is never
saved.

### Custom WAV files
The chosen path is saved as it is. If the file later goes missing, Settings shows a warning, the path
is **kept** (never replaced silently), and the alarm plays the Windows default so it is still heard;
the Events log says so. Your file is only read; volume below 100% plays a scaled copy from
`%LOCALAPPDATA%\ARES Battery Guardian\cache\sounds\`. **Test alarm** plays the sound and volume
currently selected, even before saving.

---

## 3. Files changed in V2

New: `app/core/smart_wake.py`, `app/core/power_request.py`, `app/core/power_probe.py`, `tests/test_smart_wake.py`,
`tests/test_persistent_settings.py`, this document.

Changed: `main.py`, `app/__init__.py` (2.0.0), `app/paths.py`, `app/database/database.py`,
`app/services/sound_service.py`, `app/ui/settings.py`, `app/ui/background_app.py`,
`app/ui/lock_window.py`, `app/ui/main_window.py`, `app/ui/tray.py`, `app/ui/theme.py`,
`app/ui/dashboard.py`, `requirements.txt`, `build_exe.bat`, `README.md`, `tests/test_protection.py`, `tests/test_startup.py`,
`tests/test_strict_lock.py`.

---

## 4. Test results

| Where | Ran | Passed | Failed | Skipped |
|---|---|---|---|---|
| Linux, CPython 3.14.3, PySide6-Essentials 6.12.0, shiboken6 6.12.0, PyInstaller 6.22.3, pyinstaller-hooks-contrib 2026.10 (the exact `requirements.txt` set), offscreen Qt | 182 | 182 | 0 | 0 |
| Windows 11 (your laptop) | not run yet | – | – | – |

The first V2 report (177 tests) came from Python 3.13 with PySide6 6.12.0 while `requirements.txt`
still said `PySide6-Essentials>=6.7,<6.11`; that mismatch is fixed and the run above replaces it.

### Versions (why these)
The laptop's `.venv\pyvenv.cfg` shows **CPython 3.14.3**, and its site-packages hold **PySide6
6.12.0** (full package, including PySide6-Essentials 6.12.0), **PyInstaller 6.22.3** and
pyinstaller-hooks-contrib 2026.10. The existing `release\` EXE was built with that setup
(`python314.dll`). `requirements.txt` now pins exactly those versions, so tests, the source run and
a future EXE build use the same stack.

| Component | Version | Compatibility checked |
|---|---|---|
| Python | 3.14.x, 64-bit | PySide6 6.12.0 needs 3.10–3.15; PyInstaller 6.22.3 supports 3.8–3.15 |
| PySide6-Essentials / shiboken6 | 6.12.0 | PyPI wheel `cp310-abi3-win_amd64` exists |
| PyInstaller / hooks-contrib | 6.22.3 / 2026.10 | the PySide6 hooks are part of PyInstaller |

`build_exe.bat` now creates its `.venv` with `py -3.14`.

PyInstaller check: a trial build of `ARES-Battery-Guardian.spec` with these exact versions completed
on **Linux**, and the resulting binary ran `--version`, `--wake-check`, a tray-only start and
`--exit` correctly. That binary was deleted. It does not prove the Windows build; the Windows EXE
has **not** been rebuilt, as requested.

All Smart Wake-Up tests are **simulated**: fake Task Scheduler, injected battery/charger readings,
fake `GetPwrCapabilities` data and a recorded `SetThreadExecutionState`. They prove ARES' logic,
the task definition and the process wiring, not that the hardware wakes. On Windows, one test
(`test_real_scheduler_is_inert_off_windows`) is skipped by design.

Run on Windows:

```bat
cd C:\Users\<you>\OneDrive\Desktop\batterysystem\ARES-Battery-Guardian
.venv\Scripts\activate
python -m unittest discover -s tests -v > test_results.txt 2>&1
type test_results.txt | findstr /R "^Ran OK FAILED"
```

---

## 5. Manual tests on the laptop (Latitude E7470 and Latitude 5480)

Do these on each laptop. Note the results; nothing here changes Windows settings unless a step
says so.

### 5.1 Check what the PC supports (no changes)
1. In a terminal: `powercfg /a`. Note whether *Standby (S3)* or *Standby (S0 Low Power Idle)* is
   listed. Both laptops normally report S3.
2. `powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE`: the last two hex values are *Allow wake timers*
   on charger (AC) and on battery (DC): 0 = disabled, 1 = enabled, 2 = important only.
3. Open ARES → Settings → G and compare *Wake-timer support* and *Sleep state* with steps 1–2.
4. If you want low-battery wakes on battery, set *Control Panel → Power Options → Change plan
   settings → Change advanced power settings → Sleep → Allow wake timers → On battery: Enable*
   yourself. ARES never changes it.
5. In the Dell BIOS (F2), note the *Power Management* options (for example *Block Sleep* and
   *Auto On Time*). I haven't verified how these interact with Windows wake timers on the E7470
   or 5480, so record what they are set to if a wake doesn't happen.

### 5.2 Task registration
1. Settings → G → tick *Wake the PC…* → **Save settings**. Wait 5 s.
2. **Test wake configuration**: it should report the task registered, its next check, *wake* allowed
   and the command pointing at this copy of ARES.
3. `schtasks /Query /TN "ARES Battery Guardian Wake Check" /V /FO LIST`: one task, *Next Run Time*
   about 5 min ahead. `powercfg /waketimers` (as administrator) should list the ARES task.
4. Untick in the tray menu → the task disappears (`schtasks /Query …` says it doesn't exist).
   Turn it on again for the next tests.

### 5.3 High-battery wake (charger connected)
1. Charge to about 85–88% with the charger connected and ARES running.
2. Put the PC to Sleep (Start → Power → Sleep). Leave it closed or idle for 10–20 min.
3. Expected: the PC wakes briefly every check; when the battery reaches 89% ARES keeps it awake; at
   90% the High Battery Protection screen and alarm appear. Unplugging releases it.
4. Check Events (smart_wake entries) and Settings → G → *Last wake check*. Note whether each wake
   happened and how late.

### 5.4 Low-battery wake (on battery)
1. Discharge to about 27–30% on battery, with *Allow wake timers* enabled on battery.
2. Sleep the PC for 10–30 min.
3. Expected behavior (experimental, not yet verified):
   If Windows and the laptop hardware support the scheduled wake timer,
   the PC may wake and run the battery check.
   Successful wake-up and alarm display have not yet been confirmed
   on the tested Dell Latitude E7470.

### 5.5 Back to sleep and battery cost
1. After a wake with no trigger, don't touch the PC: it should return to Sleep after Windows'
   unattended timeout (about 2 min). ARES never forces Sleep.
2. Over a night on battery with Smart Wake-Up on, compare the battery drop with a night with it off.

### 5.6 Limits
1. Hibernate (`shutdown /h`) and Shut down: no wake is expected. After you start the PC again the
   missed check runs as soon as possible and is reported as late.
2. `ARES-Battery-Guardian.exe --disable-wake` turns it off and removes the task immediately.

### 5.7 Persistent settings
1. Change several settings in every section (including a custom .wav and its volume, theme Light,
   wake interval 10), click **Save settings**, and check *✓ All changes saved*.
2. Exit ARES (tray), start it again: all values are as saved, the window opens at the same size,
   position and tab.
3. Restart Windows: same check after ARES starts at sign-in.
4. Rename the custom .wav: Settings shows the warning, the path is kept, Test alarm plays the
   default sound and says so.
5. Replace the program folder with a fresh copy (simulated update): settings and history remain
   (they live in `%LOCALAPPDATA%\ARES Battery Guardian\`).
6. **Restore default settings…** → Yes: settings are defaults, the History tab still has your data.
