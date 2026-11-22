# Dashboard compatibility notice moved to Hardware Diagnostics (V2 Beta)

## What changed

On a computer where ARES found no supported firmware charge-control interface (for example a
Latitude without Dell Command | Configure), the Dashboard used to show:

- the line "Monitoring only: no supported charge-control interface, so ARES cannot stop or resume
  charging…", and
- the detection notes under it (Dell BIOS WMI not present, Dell Command | Configure not
  installed, the firmware threshold limits).

These are no longer on the Dashboard. The Dashboard now shows:

- the headline with the real charging state from the battery driver, as before;
- in the **Charge control** card: *Hardware control: Not available on this PC (monitoring
  only)*, *Interface: None found (details: Settings > Hardware Diagnostics)* and *Firmware target:
  N/A (no hardware control)*. The Dashboard therefore never suggests that ARES controls charging.

When charge control **is** supported, the Dashboard is unchanged, including its action messages
such as "BIOS password required".

## Settings > I. Hardware Diagnostics (new, read-only)

- Computer and BIOS version
- Charge control: Supported / Read-only / Not available (monitoring only)
- Interface found, access (read, write, administrator rights, BIOS password)
- Firmware limits and the current firmware setting
- Status: the full status text, including the "Monitoring only…" explanation
- Detection notes: every note from hardware detection and the controller (duplicates removed)
- **Re-detect hardware** runs detection again. It uses the existing re-detect path, which only
  reads hardware information.

It has no settings, adds nothing to the database and changes nothing when viewed.

## What did not change

Hardware detection (`app/hardware/*`, `app/core/hardware_detector.py`), the charging controller and
its status messages, monitoring, alarms, protection screens, Settings section A (which still says
that ARES cannot stop charging on this PC), the database, saved settings and battery history.

## Tests

`tests/test_dashboard_compat_notice.py` (7): the notice and Dell notes are hidden on the Dashboard
in monitoring-only mode; the Dashboard never shows "Supported via" or a firmware target there;
supported mode still shows its detail and action messages; switching between modes; the
Diagnostics section shows computer, BIOS, support, interface, status and all notes once;
supported details (access, limits, firmware setting); the Re-detect button emits the existing
signal and the section adds no setting.
