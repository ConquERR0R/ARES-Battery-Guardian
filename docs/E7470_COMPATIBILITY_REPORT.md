# Hardware Compatibility Report: Dell Latitude E7470

## Status: Tested — Monitoring Only

This report documents the observed behavior of ARES Battery Guardian on a Dell Latitude E7470.

**Tested configuration:**
- Laptop: Dell Latitude E7470
- BIOS version: 1.18.5
- Operating system: Windows 11
- Battery monitoring: Working
- Firmware charge control: Not verified as available; ARES reports monitoring-only mode.

## 1. Summary

| Capability | Observed result |
|---|---|
| Read battery percentage | Supported |
| Detect AC connection | Supported |
| Read charging/discharging state | Supported |
| Display battery voltage and capacity | When reported by the driver |
| Display battery health and wear | Based on available battery data |
| High Battery Protection alert | Tested |
| Low Battery Protection alert | Software protection feature |
| Firmware charge-control writes | Not confirmed on this laptop |
| Automatic physical charging stop/resume | Not supported by the verified test result |

**Important:** ARES operates in monitoring-only mode for the tested E7470 configuration. Do not assume that it can configure BIOS charge thresholds or physically stop and resume charging on this laptop.

## 2. What ARES can do on this laptop

ARES reads battery telemetry provided by Windows and uses the available information for monitoring and notifications.

The application can:
- Display battery percentage and charging state.
- Detect whether AC power is connected.
- Record battery readings and charging history.
- Calculate battery health and wear using available capacity information.
- Display configurable high- and low-battery protection alerts.
- Record monitoring and application events.

Battery temperature and other optional values are displayed only when the hardware driver provides them.

## 3. Firmware charge-control limitations

Dell laptops may provide firmware charge-control features through BIOS settings or supported Dell management interfaces.

However, support varies by model, BIOS version, battery, and firmware configuration.

For the tested E7470:
- ARES detected the hardware as monitoring-only.
- A successful firmware charge-control write and read-back were not established.
- No physical charge-control capability should be advertised for this tested configuration.

ARES must not report that charging has paused or resumed unless actual hardware telemetry confirms the corresponding state.

ARES does not use undocumented embedded-controller register writes, Peak Shift workarounds, or fabricated charging states to simulate firmware control.

## 4. Battery protection alerts are not hardware charge control

ARES provides two software protection modes:

| Protection | Default condition | Action |
|---|---|---|
| High Battery Protection | Battery at or above 90% while AC is connected | Displays an alert asking the user to unplug the charger |
| Low Battery Protection | Battery at or below 25% while AC is disconnected | Displays an alert asking the user to connect the charger |

These are software reminders. They do not physically stop or start charging.

The actual charging behavior remains controlled by the laptop firmware and hardware.

## 5. Hardware diagnostics

Open ARES and navigate to:

**Settings → I. Hardware Diagnostics**

Review the detected hardware, firmware interface availability, and charge-control status.

When charge control is unavailable, ARES should report monitoring-only mode rather than claiming that a firmware setting has been applied.

For additional diagnostics, use the application's supported diagnostic tools and inspect the Events log.

## 6. Compatibility with other Dell laptops

This report does not establish that every Dell Latitude model has the same limitation.

Other Dell models may support charge thresholds through Dell Command | Configure or Dell BIOS WMI.

Before advertising charge-control support for another model, verify all of the following on that specific hardware:

1. The firmware exposes the required charge-control setting.
2. The application can read the existing setting.
3. The application can write the requested setting successfully.
4. The application can read the setting back and confirm it.
5. Actual charging behavior agrees with the firmware setting.
6. The behavior remains correct after restarting Windows.

A successful test on one Dell model must not be treated as proof of compatibility with another model.

## 7. Safety principles

ARES follows these principles:

- Never claim hardware control without confirmation.
- Never fabricate charging-paused or charging-resumed states.
- Never write undocumented embedded-controller registers.
- Never disable AC power as a workaround.
- Preserve monitoring functionality when hardware charge control is unavailable.
- Clearly disclose unsupported hardware features to users.

## 8. Research references

The following references may help investigate charge-control capabilities on other Dell models. They are background research, not proof that the tested E7470 supports firmware control.

- [Dell Latitude E7470 Owner's Manual](https://coloradosos.gov/pubs/elections/VotingSystems/DVS-DemocracySuite517/documentation/DellLatitudeE7470UltrabookOwnersManual.pdf)
- [Dell Command | Configure Reference Guide](https://dl.dell.com/topicspdf/command-configure-v42_reference_guide_en-us.pdf)
- [Dell Command | Configure Product Information](https://www.dell.com/support/kbdoc/en-th/000178000/dell-command-configure)
- [TLP Dell Battery Care Thresholds](https://linrunner.de/tlp/settings/bc-vendors)
- [Dell Battery Compatibility Notes](https://maniacx.github.io/Battery-Health-Charging/device-compatibility/dell)

These sources describe general Dell firmware capabilities and must not be interpreted as confirmation of functionality on the tested laptop.

---

**Last updated:** November 22, 2026

**Project:** ARES Battery Guardian