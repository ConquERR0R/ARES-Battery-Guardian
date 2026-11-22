# ARES Battery Guardian

**ARES Battery Guardian** is a Windows desktop application for monitoring laptop battery health, tracking battery activity, and providing configurable battery protection alerts.

Built with Python, PySide6, and SQLite, ARES is designed for Windows 10/11 laptops, with particular attention to Dell Latitude devices.

> **Important hardware limitation:** ARES does not guarantee physical charging control. On the tested Dell Latitude E7470, charge control is monitoring-only. Battery protection screens are software reminders and do not physically stop or resume charging.

## Features

### Battery monitoring
- Reads battery information provided by the Windows battery driver.
- Displays battery percentage, AC connection, charging status, voltage, power rate, and capacity information when available.
- Shows battery health, wear, and cycle count when supported by the hardware.
- Displays battery temperature only when the driver provides it.

### Battery protection alerts
- High Battery Protection: configurable alert when the battery reaches the high threshold while connected to AC.
- Low Battery Protection: configurable alert when the battery reaches the low threshold while disconnected from AC.
- Full-screen warning windows with configurable alarm sounds.
- Automatic release when the charger state changes as expected.
- Emergency release and recovery options.

**These protection screens are software reminders, not hardware charging controls.** They do not stop or start battery charging.

### Battery history and health
- Battery percentage history with charging and discharging information.
- Battery health and wear analysis.
- Charge and discharge sessions.
- Hover tooltips showing recorded percentage, timestamp, charging state, and available voltage/capacity information.

### Windows integration
- System tray support.
- Optional startup when signing in to Windows.
- Single-instance application behavior.
- Windows power profile integration.
- Persistent application settings.

### Optional features
- **Smart Audio Override:** intended to unmute Windows and raise master volume during critical alarms, then restore the previous audio state. Device-specific behavior is still being validated; do not rely on this feature as the only way to hear an alarm.
- **Smart Wake-Up Protection:** experimental. Uses a Windows Task Scheduler wake timer to check battery status during sleep. Actual wake behavior depends on Windows power settings and hardware and is not guaranteed.

## Hardware compatibility

ARES uses Windows battery telemetry and detects whether supported hardware charge-control interfaces are available.

- **Dell Latitude E7470:** tested for battery monitoring and protection alerts. Hardware charge control is reported as monitoring-only on this device.
- **Other Dell laptops:** charge-control availability depends on the specific model, BIOS, firmware, and supported Dell interfaces.
- **Other Windows laptops:** monitoring availability depends on the battery driver and hardware telemetry provided by the device.

ARES must never report that charging has physically paused or resumed unless supported telemetry confirms it.

See `docs/` for hardware diagnostics and compatibility information.

## Requirements

- Windows 10 or Windows 11, 64-bit.
- Python 3.14 (64-bit) when running from source.
- Compatible laptop battery hardware and Windows battery driver.

The packaged installer does not require users to install Python.

## Installation

### Option 1: Install the packaged application

Download the latest available ARES installer from the project's GitHub Releases page.

1. Download the installer.
2. Verify its SHA256 checksum when one is provided.
3. Run the installer.
4. Follow the installation instructions.

Windows security systems may display warnings for new or unsigned applications. Only install releases obtained from the official project repository. Do not disable Windows security protections simply to run an installer.

### Option 2: Run from source

Clone the repository:

```powershell
git clone https://github.com/ConquERROR/ARES-Battery-Guardian.git
cd ARES-Battery-Guardian
```

Create and activate a virtual environment:

```powershell
py -3.14 -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the application:

```powershell
python main.py
```

Run automated tests:

```powershell
python -m unittest discover -s tests
```

## Using ARES

### Dashboard
The Dashboard displays available battery information, charging status, capacity, health, wear, and monitoring information.

Hardware features that are unavailable on the current laptop should be reported as unavailable or monitoring-only.

### Battery protection

Default protection thresholds:

| Protection | Default condition |
|---|---|
| High Battery Protection | Battery at or above 90%, charger connected |
| Low Battery Protection | Battery at or below 25%, charger disconnected |

The thresholds and alarm settings can be configured in Settings.

The warning screen is an ordinary desktop window. It is not a Windows security lock and does not prevent users from accessing Windows system controls.

### Alarm sounds

ARES supports configurable alarm sounds using WAV files.

- Each protection alarm has its own sound settings.
- Alarm volume and repeat interval can be configured.
- The original selected audio file is read, not modified.
- Missing or invalid sound files should produce a warning and use the fallback sound.

Smart Audio Override is optional and should be treated as under validation until its behavior has been confirmed on the target Windows audio devices.

### Battery history

The History screen displays recorded battery readings and charging/discharging activity. Hover over graph points to inspect available reading details.

### Hardware diagnostics

Open:

**Settings → I. Hardware Diagnostics**

This section provides information about the detected hardware and available charge-control capabilities.

## Safety and emergency recovery

ARES is a normal Windows desktop application.

- It does not install keyboard hooks or disable Windows security controls.
- It does not modify Ctrl+Alt+Del, Task Manager, or Windows security policies.
- Its full-screen protection windows are application windows, not operating-system locks.
- Hardware charging control must never be assumed unless the device explicitly supports it and the behavior is verified.

If a protection screen needs to be released:

1. Change the charger connection as indicated by the warning.
2. Use the emergency release shortcut `Ctrl+Alt+Shift+U`.
3. End ARES through Windows Task Manager if necessary.
4. Use the application's supported emergency command-line option when available.
5. Restart Windows if necessary.

Do not rely on ARES as a replacement for the laptop manufacturer's battery safety mechanisms.

## Data storage

ARES stores its application data under:

```text
%LOCALAPPDATA%\ARES Battery Guardian\
```

The application stores its SQLite database, battery history, settings, and logs in its data directory.

Updates and reinstalls are designed to preserve user data. Users should still maintain their own backups of important information.

## Project structure

```text
ARES-Battery-Guardian/
├── app/
│   ├── core/
│   ├── database/
│   ├── hardware/
│   ├── services/
│   ├── ui/
│   └── utils/
├── assets/
├── docs/
├── packaging/
├── tests/
├── main.py
├── requirements.txt
├── README.md
├── LICENSE
└── .gitignore
```

## Testing and known limitations

ARES includes automated tests for battery monitoring logic, protection behavior, settings persistence, startup handling, history tooltips, packaging, and other application components.

Automated tests do not prove that every feature works on every laptop.

Known limitations:

- Charge-control support is hardware-dependent. The tested Dell Latitude E7470 operates in monitoring-only mode.
- Smart Wake-Up is experimental; actual Windows sleep/wake behavior remains unverified.
- Smart Audio Override requires further validation across Windows audio devices.
- Battery telemetry depends on the laptop's hardware and Windows driver.
- Protection screens are reminders and cannot physically control charging.

Consult the documentation in `docs/` for additional testing details and compatibility information.

## License

ARES Battery Guardian is distributed under the MIT License. See [LICENSE](LICENSE) for details.

Contributions, bug reports, and feature suggestions are welcome.
