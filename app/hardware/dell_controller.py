"""Dell charge control through Dell-documented, supported interfaces only.

Two interfaces are supported, in order of preference:

1. Dell BIOS WMI provider (namespace root\\dcim\\sysman\\biosattributes).
   Built into the BIOS of Dell commercial systems released from ~2018 onward.
   Attributes: PrimaryBattChargeCfg (Enumeration), CustomChargeStart /
   CustomChargeStop (Integer), written through BIOSAttributeInterface.SetAttribute.
   2016-era models such as the Latitude E7470 normally do NOT have this namespace;
   the controller detects that rather than assuming.

2. Dell Command | Configure (cctk.exe), Dell's free BIOS configuration CLI, which
   supports older Latitude E-series systems including the E7470:
       cctk --PrimaryBattChargeCfg                 (read)
       cctk --PrimaryBattChargeCfg=Custom:50-90    (write)
       cctk --PrimaryBattChargeCfg=Custom:50-90 --ValSetupPwd=<pwd>  (BIOS admin password set)
   Requires administrator rights.

Once Custom mode is written, the battery's charging is stopped and resumed by the
Dell embedded controller itself. This module never touches EC registers, never
fakes state, and verifies every write by reading the value back from the BIOS.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from app.hardware.base_controller import (BaseChargeController, ChargeConfig, ControllerCapabilities,
                                          WriteResult)
from app.utils import system_info as si
from app.utils.calculations import ThresholdLimits

log = logging.getLogger(__name__)

# Dell's documented limits for Custom charge (BIOS setup, Dell Command | Configure
# reference guide and the Linux dell-laptop driver all agree):
#   Custom Charge Start: 50-95 %, Custom Charge Stop: 55-100 %, Start <= Stop - 5.
DELL_DEFAULT_LIMITS = ThresholdLimits(start_min=50, start_max=95, stop_min=55, stop_max=100, min_gap=5)

DELL_MODES = ["Standard", "Express", "Adaptive", "PrimAcUse", "Custom"]

_CFG_RE = re.compile(r"PrimaryBattChargeCfg\s*=\s*([A-Za-z]+)(?:\s*:\s*(\d{1,3})\s*-\s*(\d{1,3}))?", re.I)
_ADV_RE = re.compile(r"AdvBatteryChargeCfg\s*=\s*([A-Za-z]+)", re.I)
_PEAK_RE = re.compile(r"PeakShiftCfg\s*=\s*([A-Za-z]+)", re.I)


def normalize_mode(raw: str) -> str:
    low = raw.strip().lower()
    for m in DELL_MODES:
        if m.lower() == low:
            return m
    if low in ("expresscharge", "express_charge"):
        return "Express"
    if low in ("primarilyac", "primacuse", "primarily_ac"):
        return "PrimAcUse"
    return raw.strip()


def parse_cctk_charge_cfg(output: str) -> ChargeConfig | None:
    """Parse e.g. 'PrimaryBattChargeCfg=Custom:50-90' or 'PrimaryBattChargeCfg=Express'."""
    m = _CFG_RE.search(output or "")
    if not m:
        return None
    mode = normalize_mode(m.group(1))
    start = int(m.group(2)) if m.group(2) else None
    stop = int(m.group(3)) if m.group(3) else None
    return ChargeConfig(mode, start, stop)


def classify_cctk_error(output: str) -> tuple[str, bool, bool, bool]:
    """Returns (message, needs_password, needs_admin, option_unsupported)."""
    text = (output or "").strip()
    low = text.lower()
    needs_pwd = "password" in low and ("required" in low or "incorrect" in low or "invalid" in low
                                       or "provide" in low or "valsetuppwd" in low)
    needs_admin = any(k in low for k in ("administrator", "elevated", "privilege", "access is denied"))
    unsupported = ("not available" in low or "not supported" in low or "cannot be configured" in low
                   or "invalid option" in low or "unrecognized option" in low)
    msg = text.splitlines()[-1] if text else "cctk returned an error with no message"
    return msg[:300], needs_pwd, needs_admin, unsupported


def find_cctk(custom_path: str | None = None) -> str | None:
    candidates: list[str] = []
    if custom_path:
        candidates.append(custom_path)
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            for arch in ("X86_64", "X86"):
                candidates.append(str(Path(base) / "Dell" / "Command Configure" / arch / "cctk.exe"))
    which = shutil.which("cctk.exe") or shutil.which("cctk")
    if which:
        candidates.append(which)
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


# --------------------------------------------------------------------------- #
# Interface 1: Dell BIOS WMI
# --------------------------------------------------------------------------- #
class DellBiosWmi:
    NS = "root/dcim/sysman/biosattributes"

    name = "Dell BIOS WMI (root\\dcim\\sysman\\biosattributes)"

    def available(self) -> bool:
        if not si.IS_WINDOWS:
            return False
        rows = si.cim_query("EnumerationAttribute", self.NS, ["AttributeName", "CurrentValue"],
                            where="AttributeName='PrimaryBattChargeCfg'")
        return bool(rows)

    def read(self) -> ChargeConfig | None:
        rows = si.cim_query("EnumerationAttribute", self.NS, ["AttributeName", "CurrentValue"],
                            where="AttributeName='PrimaryBattChargeCfg'")
        if not rows:
            return None
        mode = normalize_mode(str(rows[0].get("CurrentValue") or ""))
        start = stop = None
        ints = si.cim_query("IntegerAttribute", self.NS, ["AttributeName", "CurrentValue"],
                            where="AttributeName='CustomChargeStart' OR AttributeName='CustomChargeStop'")
        for r in ints:
            if r.get("AttributeName") == "CustomChargeStart":
                start = int(r.get("CurrentValue"))
            elif r.get("AttributeName") == "CustomChargeStop":
                stop = int(r.get("CurrentValue"))
        return ChargeConfig(mode, start, stop)

    def limits(self) -> ThresholdLimits:
        rows = si.cim_query("IntegerAttribute", self.NS, ["AttributeName", "LowerBound", "UpperBound"],
                            where="AttributeName='CustomChargeStart' OR AttributeName='CustomChargeStop'")
        lim = {r.get("AttributeName"): (r.get("LowerBound"), r.get("UpperBound")) for r in rows}
        try:
            s_lo, s_hi = (int(x) for x in lim["CustomChargeStart"])
            p_lo, p_hi = (int(x) for x in lim["CustomChargeStop"])
            return ThresholdLimits(s_lo, s_hi, p_lo, p_hi, DELL_DEFAULT_LIMITS.min_gap)
        except (KeyError, TypeError, ValueError):
            return DELL_DEFAULT_LIMITS

    def admin_password_set(self) -> bool | None:
        rows = si.cim_query("PasswordObject", self.NS, ["NameId", "IsPasswordSet"], where="NameId='Admin'")
        if not rows:
            return None
        return bool(rows[0].get("IsPasswordSet"))

    def set_attributes(self, pairs: list[tuple[str, str]], password: str | None) -> tuple[bool, str]:
        """Call BIOSAttributeInterface.SetAttribute for each pair; Status 0 = success."""
        def ps_quote(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        lines = ["$ErrorActionPreference='Stop'",
                 f"$bai = Get-WmiObject -Namespace {self.NS.replace('/', chr(92))} -Class BIOSAttributeInterface"]
        if password:
            lines += [f"$b = [System.Text.Encoding]::UTF8.GetBytes({ps_quote(password)})",
                      "$t = 1; $n = $b.Length"]
        else:
            lines += ["$b = 0; $t = 0; $n = 0"]
        for name, value in pairs:
            lines.append(f"$r = $bai.SetAttribute($t, $n, $b, {ps_quote(name)}, {ps_quote(value)}); "
                         f"Write-Output (\"{name}=\" + $r.Status)")
        res = si.powershell("\n".join(lines) + "\n", timeout=60)
        out = res.stdout + res.stderr
        statuses = re.findall(r"^(\w+)=(\d+)\s*$", res.stdout, re.M)
        if not res.ok or len(statuses) != len(pairs):
            return False, (out.strip().splitlines() or ["PowerShell call failed"])[-1][:300]
        bad = [(n, s) for n, s in statuses if s != "0"]
        if bad:
            # Dell status codes: 1 failed, 2 invalid parameter, 3 access denied (password), 4 not supported
            codes = {"1": "failed", "2": "invalid parameter", "3": "access denied / wrong BIOS password",
                     "4": "not supported", "5": "memory error", "6": "protocol error"}
            return False, "; ".join(f"{n}: {codes.get(s, 'status ' + s)}" for n, s in bad)
        return True, "BIOS accepted the new values"


# --------------------------------------------------------------------------- #
# Interface 2: Dell Command | Configure
# --------------------------------------------------------------------------- #
class DellCctk:
    name = "Dell Command | Configure (cctk.exe)"

    def __init__(self, path: str) -> None:
        self.path = path

    def run(self, *args: str, timeout: float = 90) -> si.CommandResult:
        return si.run_hidden([self.path, *args], timeout=timeout)

    def read(self) -> tuple[ChargeConfig | None, str]:
        res = self.run("--PrimaryBattChargeCfg")
        out = res.stdout + "\n" + res.stderr
        cfg = parse_cctk_charge_cfg(out)
        return cfg, out

    def read_option(self, regex: re.Pattern, option: str) -> str | None:
        res = self.run(f"--{option}")
        m = regex.search(res.stdout + res.stderr)
        return m.group(1) if m else None

    def write(self, value: str, password: str | None) -> tuple[bool, str]:
        args = [f"--PrimaryBattChargeCfg={value}"]
        if password:
            args.append(f"--ValSetupPwd={password}")
        res = self.run(*args)
        out = (res.stdout + "\n" + res.stderr).strip()
        if password:
            out = out.replace(password, "********")
        return res.ok, out


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
class DellController(BaseChargeController):
    vendor = "Dell"

    def __init__(self, model: str | None = None, cctk_path: str | None = None) -> None:
        self.model = model or ""
        self._cctk_hint = cctk_path
        self._wmi: DellBiosWmi | None = None
        self._cctk: DellCctk | None = None
        self._caps = None

    @property
    def interface_name(self) -> str | None:
        if self._wmi:
            return self._wmi.name
        if self._cctk:
            return self._cctk.name
        return None

    def probe(self) -> ControllerCapabilities:
        notes: list[str] = []
        admin = si.is_admin()
        caps = ControllerCapabilities(vendor="Dell", interface=None, supported=False,
                                      requires_admin=True, available_modes=list(DELL_MODES))
        if not si.IS_WINDOWS:
            caps.notes = ["Not running on Windows."]
            self._caps = caps
            return caps

        # ---- 1. Dell BIOS WMI ---------------------------------------------
        wmi = DellBiosWmi()
        if wmi.available():
            self._wmi = wmi
            caps.interface = wmi.name
            caps.limits = wmi.limits()
            caps.password_required = wmi.admin_password_set()
            cfg = wmi.read()
            caps.can_read = cfg is not None
            caps.can_write = admin
            caps.supported = True
            if not admin:
                notes.append("Writing BIOS settings requires running ARES as administrator.")
        else:
            notes.append("Dell BIOS WMI namespace (root\\dcim\\sysman\\biosattributes) not present. "
                         "This is expected on 2016-era models such as the Latitude E7470.")

        # ---- 2. Dell Command | Configure ----------------------------------
        if self._wmi is None:
            path = find_cctk(self._cctk_hint)
            if not path:
                notes.append("Dell Command | Configure (cctk.exe) is not installed. Install it from "
                             "dell.com/support to enable charge-threshold control.")
            else:
                cctk = DellCctk(path)
                if not admin:
                    self._cctk = cctk
                    caps.interface = cctk.name
                    caps.supported = True   # tool present; actual support confirmed once elevated
                    caps.limits = DELL_DEFAULT_LIMITS
                    notes.append("cctk.exe found, but it needs administrator rights to read or write BIOS "
                                 "settings. Restart ARES as administrator (or enable the elevated startup task).")
                else:
                    cfg, out = cctk.read()
                    if cfg is not None:
                        self._cctk = cctk
                        caps.interface = cctk.name
                        caps.supported = caps.can_read = caps.can_write = True
                        caps.limits = DELL_DEFAULT_LIMITS
                        adv = cctk.read_option(_ADV_RE, "AdvBatteryChargeCfg")
                        if adv and adv.lower() == "enabled":
                            notes.append("Advanced Battery Charge Configuration is ENABLED in BIOS. While it is "
                                         "on, the firmware ignores Primary Battery Charge Configuration. Disable "
                                         "it in BIOS Setup (F2 > Power Management) to use custom thresholds.")
                        peak = cctk.read_option(_PEAK_RE, "PeakShiftCfg")
                        if peak and peak.lower() == "enabled":
                            notes.append("Peak Shift is ENABLED: during its schedule the laptop runs from battery "
                                         "even on AC.")
                    else:
                        msg, need_pwd, need_admin, unsupported = classify_cctk_error(out)
                        if unsupported:
                            notes.append(f"cctk reports PrimaryBattChargeCfg is not available on this system: {msg}")
                        else:
                            notes.append(f"cctk could not read the charge configuration: {msg}")

        if caps.supported:
            notes.append("Dell firmware limits: Custom Charge Start 50-95%, Custom Charge Stop 55-100%, "
                         "start at least 5% below stop. A 25% start threshold is NOT possible on Dell "
                         "firmware; the closest is 50%.")
            notes.append("The Dell embedded controller enforces the thresholds autonomously, so control "
                         "keeps working even when ARES is closed.")
        caps.notes = notes
        self._caps = caps
        return caps

    def read_config(self) -> ChargeConfig | None:
        self.capabilities  # ensure probed
        if self._wmi:
            return self._wmi.read()
        if self._cctk:
            cfg, out = self._cctk.read()
            if cfg is None:
                log.info("cctk read failed: %s", classify_cctk_error(out)[0])
            return cfg
        return None

    def write_thresholds(self, start: int, stop: int, password: str | None = None) -> WriteResult:
        caps = self.capabilities
        if not caps.supported:
            return WriteResult(False, False, "No supported Dell interface. No command was sent.")
        if not si.is_admin():
            return WriteResult(False, False, "Administrator rights are required to change BIOS charge settings.",
                               needs_admin=True)
        limits = caps.limits or DELL_DEFAULT_LIMITS
        if not (limits.start_min <= start <= limits.start_max and limits.stop_min <= stop <= limits.stop_max
                and stop - start >= limits.min_gap):
            return WriteResult(False, False, f"Refused: {start}-{stop}% is outside Dell's accepted range "
                                             f"(start {limits.start_min}-{limits.start_max}, stop "
                                             f"{limits.stop_min}-{limits.stop_max}, gap >= {limits.min_gap}).")
        before = self.read_config()
        if before and before.matches(start, stop):
            return WriteResult(True, True, "Firmware already has these thresholds; nothing sent.", before)

        if self._wmi:
            # Write in an order that never creates an invalid intermediate (start >= stop) state.
            pairs = [("CustomChargeStop", str(stop)), ("CustomChargeStart", str(start))]
            if before and before.stop is not None and stop < before.stop:
                pairs.reverse()
            pairs.append(("PrimaryBattChargeCfg", "Custom"))
            ok, msg = self._wmi.set_attributes(pairs, password)
            need_pwd = (not ok) and "password" in msg.lower()
        else:
            assert self._cctk is not None
            ok, out = self._cctk.write(f"Custom:{start}-{stop}", password)
            msg, need_pwd, need_admin, _ = classify_cctk_error(out) if not ok else (
                (out.splitlines() or ["OK"])[-1], False, False, False)
            if need_admin:
                return WriteResult(False, False, msg, needs_admin=True)

        after = self.read_config()
        verified = bool(after and after.matches(start, stop))
        if ok and not verified:
            msg = (f"The interface reported success but the BIOS read-back is "
                   f"{after.describe() if after else 'unavailable'}; treating as NOT applied.")
        return WriteResult(ok and verified, verified, msg if not verified else
                           f"Firmware accepted and confirmed Custom {start}-{stop}%.", after, needs_password=need_pwd)

    def restore_mode(self, config: ChargeConfig, password: str | None = None) -> WriteResult:
        if config.is_custom and config.start is not None and config.stop is not None:
            return self.write_thresholds(config.start, config.stop, password)
        if not self.capabilities.supported:
            return WriteResult(False, False, "No supported Dell interface. No command was sent.")
        if not si.is_admin():
            return WriteResult(False, False, "Administrator rights are required.", needs_admin=True)
        mode = normalize_mode(config.mode)
        if self._wmi:
            ok, msg = self._wmi.set_attributes([("PrimaryBattChargeCfg", mode)], password)
            need_pwd = (not ok) and "password" in msg.lower()
        else:
            assert self._cctk is not None
            ok, out = self._cctk.write(mode, password)
            msg, need_pwd, _, _ = classify_cctk_error(out) if not ok else ("OK", False, False, False)
        after = self.read_config()
        verified = bool(after and after.mode.lower() == mode.lower())
        return WriteResult(ok and verified, verified,
                           f"Restored {mode} mode." if verified else f"Restore not confirmed: {msg}",
                           after, needs_password=need_pwd)
