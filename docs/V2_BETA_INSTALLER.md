# ARES Battery Guardian 2.0.0 Beta 1: Windows installer

`ARES-Battery-Guardian-V2-Beta-Setup.exe` installs ARES for the current Windows user. Users do
not need Python, pip, VS Code or PowerShell: the PyInstaller build carries Python 3.14, PySide6 /
Qt and every module ARES uses.

## What the installer does

| | |
|---|---|
| Program folder | `%LOCALAPPDATA%\Programs\ARES Battery Guardian` (per user, **no administrator rights**) |
| Data folder (unchanged) | `%LOCALAPPDATA%\ARES Battery Guardian\{data,logs,cache}`: settings, history, sessions, events, custom alarm paths, database |
| Shortcuts | Start Menu, plus Desktop (an option on the "Additional tasks" page, ticked by default) |
| Start at sign-in | Controlled by ARES's own **Start with Windows** setting (on by default; an existing choice is kept). On its first start ARES points the HKCU Run entry at the installed EXE. The installer itself never changes this setting. |
| Update | Run a newer Setup over it. It asks a running ARES to exit cleanly (`--exit`), replaces the program files (the old `_internal` folder is removed first so two builds never mix), and leaves the data folder alone. |
| Uninstall | Settings → Apps, or Start Menu. Asks ARES to exit, removes the Windows sign-in entry and the Smart Wake-Up task **only if they start this copy** (`--uninstall-cleanup`), then removes the program files and shortcuts. **The data folder is kept.** A reinstall continues with the same settings and history. |
| Safety guard | Setup refuses a program folder that is inside, or contains, the data folder. The script has no `[UninstallDelete]` section. |
| Beta notes | Shown before installing and copied to the program folder as `BETA_NOTES.txt` |

Version information: the title bar, the tray tooltip and the new **About** tab show
"2.0.0 Beta 1". The About tab also shows the build date, Python / PySide6 / Qt / PyInstaller
versions, the program path and the data folder, with a "Copy" button for bug reports. Smart
Wake-Up is labelled **Experimental** in Settings, in the tray menu and on the About page.

## Building

Requirements on the build PC: the project's `.venv` (Python 3.14 with exactly the versions in
`requirements.txt`) and Inno Setup 6.

```bat
packaging\build_installer.bat          REM environment check, unit tests, PyInstaller, packaged-EXE checks, Setup.exe
packaging\build_installer.bat test     REM ... plus the install / update / uninstall / reinstall test
```

| Step | Log (`packaging\logs\`) |
|---|---|
| 1. `.venv` matches `requirements.txt` exactly (nothing is installed) | `01_environment.log` |
| 2. Unit tests | `02_unit_tests.log` |
| 3. PyInstaller → `dist_v2beta\ARES-Battery-Guardian\` | `03_pyinstaller.log` |
| 4. Packaged EXE checks (`verify_package.py`) | `04_verify_package.log` |
| 5. Inno Setup → `installer_output\ARES-Battery-Guardian-V2-Beta-Setup.exe` | `05_inno_setup.log`, `setup_sha256.txt` |
| 6. Real-hardware Smart Audio Override check (`audio_hw_check.py`, no sound) and install test (`test_installer.py`), both with `test` only | `07_audio_hw_check.log`, `06_test_installer.log`, `06_setup_*.log`, `06_uninstall.log` |

The script writes only to `build_v2beta\`, `dist_v2beta\`, `installer_output\` and
`packaging\logs\`. The original `build_exe.bat`, `ARES-Battery-Guardian.spec`, `build\`, `dist\`,
`release\`, `data\`, `logs\` and `.venv\` are not changed. Copies of the original build files
are kept in `packaging\backup-2026-11-21\`.

### How the tests protect your data

- Every launch of the packaged EXE uses a **new temporary `ARES_DATA_DIR`**. Your real settings
  and history are never opened, and the startup and wake-task management of the real app is off
  for those runs.
- The real `%LOCALAPPDATA%\ARES Battery Guardian\data` folder is hashed before the test and after
  every step. The database is read from a temporary copy, never in place.
- With `ARES_DATA_DIR` set, ARES never creates, moves or deletes the Windows wake task. That task is shared by the whole Windows user and always checks the normal data folder. A first Windows test run showed why this matters: a unit test of `--wake-check` left a real task behind. That is fixed, and `tests/test_smart_wake.py::SeparateDataFolderTest` guards it.
- The real HKCU Run value is saved and restored exactly. A check that would delete a real Smart
  Wake-Up task or elevated startup task is skipped and reported as SKIP.
- Setup and the uninstaller run with `ARES_DATA_DIR` set to a temporary folder, so their
  "exit the running ARES" step reaches only test instances. The install test leaves ARES
  2.0.0 Beta 1 installed at the end.

### What `verify_package.py` checks

- **Bundle contents:** Qt platform plugin `qwindows.dll`, the style plugin, the `.ico` image
  plugin, the Python runtime, QtNetwork (single instance), sqlite3, the icon and
  `build_info.json`. No database is bundled.
- **Windows version resource:** FileVersion 2.0.0.1, ProductVersion "2.0.0 Beta 1".
- **Command-line arguments:**
  - `--diagnose --json`.
  - `--wake-check`, with Smart Wake-Up off and on.
  - `--wake-check` and `--disable-wake` on a test folder: the setting is turned off, and the real wake task is not created, moved or removed.
  - `--uninstall-cleanup`: this copy's entries are removed and another copy's entries are kept. Nothing is written to the data folder.
  - `--install-startup` / `--remove-startup`.
  - `--autostart` with "start minimized": tray only, one monitoring loop.
  - A second launch hands over to the running instance.
  - `--exit`, and a normal launch that opens the dashboard.

## Test results

**First Windows run (2026-11-21, Latitude, non-elevated):**
- `.venv` matched `requirements.txt` exactly: Python 3.14.3, PySide6-Essentials 6.12.0, shiboken6 6.12.0, PyInstaller 6.22.3, hooks-contrib 2026.10.
- Unit tests: 193 run, 1 failed, 1 skipped. Inno Setup 6 was not installed. The build stopped before PyInstaller.
- The failure was in the test, not in the Settings save logic. On Windows the "Start with Windows" box shows the real registration, so the test's fake registry and its way of setting the box were fixed.
- That run also left the real task `ARES Battery Guardian Wake Check` on the PC. It came from the `--wake-check` unit test and is fixed (see above).
- Second Windows run: Inno Setup 6.7.3 installed (per user, via winget). 196 run, 1 failed, 2 skipped. The failure was `LockWithBackgroundAppTest`, whose monitoring thread read the laptop's real battery and ran real Dell detection. Tests that run the whole background app now use a fake battery and test machine (`tests/fake_hw.py`).
- Linux, with Smart Audio Override added: 224 tests, 223 passed, 1 skipped (Windows-only).
- Linux, with the History hover tooltip added: 241 tests, 240 passed, 1 skipped (Windows-only).
- Linux, with the Dashboard notice moved to Hardware Diagnostics: 248 tests, 247 passed, 1 skipped (Windows-only).

See the **Windows build** section below. It is filled in from the logs of the build on the
Latitude.

## Not verified

- **Smart Wake-Up hardware wake** stays **experimental**. No test has put a laptop to sleep and
  confirmed that Windows woke it for the check. Use the manual sleep test in
  `docs/V2_SMART_WAKE_AND_SETTINGS.md`.
- **Packaged EXE registering the wake task:** the packaged EXE creating the wake task from the
  Settings page is covered by unit tests with a fake Task Scheduler only. The packaged tests
  register a test task themselves and check that the EXE removes it.
- **Elevated startup task** (administrator mode) is not exercised by the installer tests. The
  installer is per-user and does not need administrator rights.
- **First start against your real data:** the tests never open it. The first time you start the
  installed ARES, it uses your existing data folder and points the Windows sign-in entry at the
  installed EXE (if "Start with Windows" is on).
- **Unsigned EXE and Setup:** Windows SmartScreen may warn. Click *More info → Run anyway*.
