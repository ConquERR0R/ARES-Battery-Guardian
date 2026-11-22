"""Smart Wake-Up Protection: wake a sleeping laptop periodically to check the battery.

How it works (supported Windows mechanisms only)
-----------------------------------------------
* One Task Scheduler task for the current user, "ARES Battery Guardian Wake Check", with
  "Wake the computer to run this task" (WakeToRun). No administrator rights, drivers, hooks or
  changes to Windows settings. Its action is `<ARES> --wake-check`.
* The task has two time triggers: the NEXT check (rescheduled by every check, so the interval
  can adapt to the battery level) and an hourly backstop, so the chain can never die silently.
  StartWhenAvailable makes Windows run a check that was missed (PC off / hibernated) as soon as
  possible; MultipleInstancesPolicy=IgnoreNew + a lock file prevent overlapping checks.
* The check process is small: it reads GetSystemPowerStatus (percent + charger), decides, and
  if a trigger is reached it activates the running ARES (single-instance socket) or starts it.
  Otherwise it just records the result and exits; ARES never forces the PC back to Sleep:
  after an unattended timer wake Windows returns to Sleep by itself after its idle timeout,
  and does not if the user is active.

What it can NOT do (reported in Settings)
* Wake a PC that is shut down (S5); no desktop program can, and nothing runs while it is off.
* Hibernate (S4) and some Modern Standby systems may not honour wake timers.
* Wake timers can be disabled in the power plan ("Allow wake timers"), often on battery; ARES
  reports that state and never changes it.
* Each wake costs battery; Windows may delay or skip wakes.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from app.utils import system_info as si

log = logging.getLogger(__name__)

TASK_NAME = "ARES Battery Guardian Wake Check"
WAKE_ARG = "--wake-check"
MIN_INTERVAL_MIN = 5           # never wake more often than this
MAX_INTERVAL_MIN = 60          # slowest adaptive interval (= backstop period)
BACKSTOP_MIN = 60
MAX_CHECKS_PER_HOUR = 12       # hard cap against wake loops
STOP_ON_BATTERY_BELOW = 5      # below this % on battery, only the hourly backstop remains
LOCK_STALE_S = 120

SUB_SLEEP = "238C9FA8-0AAD-41ED-83F4-97BE242C8F20"
RTCWAKE = "BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D"
RTC_VALUES = {0: "disabled", 1: "enabled", 2: "important only"}


# --------------------------------------------------------------------------------------------
# Capability detection
# --------------------------------------------------------------------------------------------
@dataclass
class WakeSupport:
    windows: bool
    sleep_state: str = "unknown"                 # what Sleep means on this PC
    s3: bool = False
    modern_standby: bool = False
    hibernate: bool = False
    rtc_wake_state: str = "unknown"              # deepest state the RTC (wake timers) can wake from
    wake_alarm_present: bool | None = None
    timers_ac: str | None = None                 # "enabled" / "disabled" / "important only" / None
    timers_dc: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def can_wake_from_sleep(self) -> bool:
        return self.windows and (self.s3 or self.modern_standby) and self.wake_alarm_present is not False

    @property
    def usable_on_ac(self) -> bool:
        return self.can_wake_from_sleep and self.timers_ac == "enabled"

    @property
    def usable_on_battery(self) -> bool:
        return self.can_wake_from_sleep and self.timers_dc == "enabled"

    def summary(self) -> str:
        if not self.windows:
            return "Not available: Smart Wake-Up needs Windows."
        parts = [f"Sleep: {self.sleep_state}", f"RTC wake from: {self.rtc_wake_state}",
                 f"wake timers on charger: {self.timers_ac or 'unknown'}",
                 f"on battery: {self.timers_dc or 'unknown'}"]
        return " · ".join(parts)

    def problems(self) -> list[str]:
        out = list(self.notes)
        if not self.windows:
            return out
        if not self.can_wake_from_sleep:
            out.append("This PC reports no sleep state that wake timers can wake from.")
        if self.timers_ac != "enabled":
            out.append(f"'Allow wake timers' on charger is {self.timers_ac or 'unknown'}: high-battery wake "
                       "checks may not run while asleep.")
        if self.timers_dc != "enabled":
            out.append(f"'Allow wake timers' on battery is {self.timers_dc or 'unknown'}: low-battery wake "
                       "checks will not run while asleep on battery. Enable it in Control Panel > Power "
                       "Options > Change plan settings > Advanced > Sleep > Allow wake timers.")
        if self.modern_standby:
            out.append("Modern Standby PC: Windows decides when scheduled tasks run during standby.")
        return out


class _SYSTEM_POWER_CAPABILITIES(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ubyte) for n in (
        "PowerButtonPresent", "SleepButtonPresent", "LidPresent", "SystemS1", "SystemS2", "SystemS3",
        "SystemS4", "SystemS5", "HiberFilePresent", "FullWake", "VideoDimPresent", "ApmPresent",
        "UpsPresent", "ThermalControl", "ProcessorThrottle", "ProcessorMinThrottle",
        "ProcessorMaxThrottle", "FastSystemS4", "Hiberboot", "WakeAlarmPresent", "AoAc",
        "DiskSpinDown", "HiberFileType", "AoAcConnectivitySupported")] + [
        ("spare3", ctypes.c_ubyte * 6), ("SystemBatteriesPresent", ctypes.c_ubyte),
        ("BatteriesAreShortTerm", ctypes.c_ubyte), ("BatteryScale", ctypes.c_uint32 * 6),
        ("AcOnLineWake", ctypes.c_int), ("SoftLidWake", ctypes.c_int), ("RtcWake", ctypes.c_int),
        ("MinDeviceWakeState", ctypes.c_int), ("DefaultLowLatencyWake", ctypes.c_int)]


POWER_STATE_NAMES = {0: "unspecified", 1: "working (none)", 2: "S1", 3: "S2", 4: "S3 Sleep",
                     5: "S4 Hibernate", 6: "S5 Off"}


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g


def _read_rtc_wake_api() -> tuple[str | None, str | None]:
    """'Allow wake timers' (AC, DC) for the active plan via powrprof (language-independent)."""
    pp = ctypes.windll.powrprof
    active = ctypes.POINTER(_GUID)()
    if pp.PowerGetActiveScheme(None, ctypes.byref(active)) != 0:
        return None, None
    try:
        sub, setting = _GUID.of(SUB_SLEEP), _GUID.of(RTCWAKE)
        ac, dc = ctypes.c_uint32(), ctypes.c_uint32()
        r1 = pp.PowerReadACValueIndex(None, active, ctypes.byref(sub), ctypes.byref(setting), ctypes.byref(ac))
        r2 = pp.PowerReadDCValueIndex(None, active, ctypes.byref(sub), ctypes.byref(setting), ctypes.byref(dc))
        return (RTC_VALUES.get(ac.value, f"value {ac.value}") if r1 == 0 else None,
                RTC_VALUES.get(dc.value, f"value {dc.value}") if r2 == 0 else None)
    finally:
        ctypes.windll.kernel32.LocalFree(active)


def parse_powercfg_rtc(text: str) -> tuple[str | None, str | None]:
    """Fallback parser for `powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE`. Works in any
    display language: the AC and DC indexes are the last two 0x######## values."""
    vals = re.findall(r"0x([0-9a-fA-F]{8})", text or "")
    if len(vals) < 2:
        return None, None
    ac, dc = (int(v, 16) for v in vals[-2:])
    return RTC_VALUES.get(ac, f"value {ac}"), RTC_VALUES.get(dc, f"value {dc}")


def support_from_caps(caps, timers: tuple[str | None, str | None], windows: bool = True) -> WakeSupport:
    ws = WakeSupport(windows=windows)
    ws.s3, ws.modern_standby = bool(caps.SystemS3), bool(caps.AoAc)
    ws.hibernate = bool(caps.SystemS4 and caps.HiberFilePresent)
    ws.wake_alarm_present = bool(caps.WakeAlarmPresent)
    ws.rtc_wake_state = POWER_STATE_NAMES.get(int(caps.RtcWake), str(caps.RtcWake))
    ws.sleep_state = ("Modern Standby (S0 Low Power Idle)" if ws.modern_standby else
                      "S3 Standby" if ws.s3 else "no sleep state available")
    ws.timers_ac, ws.timers_dc = timers
    if ws.hibernate:
        ws.notes.append("Hibernate is available; wake-up from Hibernate is not guaranteed.")
    return ws


def detect() -> WakeSupport:
    """Read the PC's sleep/wake capabilities. Read-only; never raises."""
    if not si.IS_WINDOWS:
        return WakeSupport(windows=False, notes=["Smart Wake-Up uses Windows Task Scheduler wake timers."])
    try:
        caps = _SYSTEM_POWER_CAPABILITIES()
        if not ctypes.windll.powrprof.GetPwrCapabilities(ctypes.byref(caps)):
            raise OSError("GetPwrCapabilities failed")
        try:
            timers = _read_rtc_wake_api()
        except Exception:
            timers = (None, None)
        if None in timers:
            res = si.run_hidden(["powercfg", "/query", "SCHEME_CURRENT", SUB_SLEEP, RTCWAKE], timeout=15)
            timers = parse_powercfg_rtc(res.stdout) if res.ok else timers
        return support_from_caps(caps, timers)
    except Exception as exc:
        log.exception("Wake capability detection failed")
        return WakeSupport(windows=True, notes=[f"Could not read power capabilities: {exc}"])


# --------------------------------------------------------------------------------------------
# Decisions (pure)
# --------------------------------------------------------------------------------------------
@dataclass
class CheckDecision:
    action: str | None          # "low", "high" or None
    reason: str
    next_minutes: int | None    # None = Smart Wake-Up off: remove the task


def base_interval(settings: dict) -> int:
    return max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, int(settings.get("wake_interval_min", 5))))


def decide(percent: float | None, ac: bool | None, settings: dict, recent: list[float], now: float) -> CheckDecision:
    if not settings.get("wake_enabled"):
        return CheckDecision(None, "Smart Wake-Up is off", None)
    base = base_interval(settings)
    if sum(1 for t in recent if now - t < 3600) > MAX_CHECKS_PER_HOUR:
        return CheckDecision(None, f"More than {MAX_CHECKS_PER_HOUR} checks in the last hour; slowing down",
                             MAX_INTERVAL_MIN)
    if percent is None or ac is None:
        return CheckDecision(None, "Battery or charger state unavailable; nothing triggered", base)
    low_on, high_on = bool(settings.get("wake_low_enabled")), bool(settings.get("wake_high_enabled"))
    low_thr, high_thr = int(settings.get("wake_low_threshold", 25)), int(settings.get("wake_high_threshold", 89))
    if ac is False:
        if low_on and percent <= low_thr:
            return CheckDecision("low", f"Battery {percent:.0f}% <= {low_thr}% on battery", base)
        if percent <= STOP_ON_BATTERY_BELOW:
            return CheckDecision(None, f"Battery {percent:.0f}%: wake checks paused on battery to save power",
                                 BACKSTOP_MIN)
        margin = percent - low_thr if low_on else 100
        mult = 6 if margin > 30 else 3 if margin > 15 else 1      # battery-drain safeguard
        return CheckDecision(None, f"Battery {percent:.0f}% on battery; no trigger", min(MAX_INTERVAL_MIN, base * mult))
    if high_on and percent >= high_thr:
        return CheckDecision("high", f"Battery {percent:.0f}% >= {high_thr}% on charger", base)
    margin = high_thr - percent if high_on else 100
    mult = 6 if margin > 40 else 3 if margin > 20 else 1
    return CheckDecision(None, f"Battery {percent:.0f}% on charger; no trigger", min(MAX_INTERVAL_MIN, base * mult))


# --------------------------------------------------------------------------------------------
# Task Scheduler
# --------------------------------------------------------------------------------------------
def check_command() -> tuple[str, list[str]]:
    from app.core import startup_manager

    exe, args = startup_manager.launch_command()
    return exe, [a for a in args if a != startup_manager.AUTOSTART_ARG] + [WAKE_ARG]


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_task_xml(exe: str, args: list[str], next_run: datetime, user: str) -> str:
    start = next_run.replace(microsecond=0).isoformat()
    backstop = (next_run + timedelta(minutes=BACKSTOP_MIN)).replace(microsecond=0).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>ARES Battery Guardian Smart Wake-Up battery check</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled></TimeTrigger>
    <TimeTrigger><StartBoundary>{backstop}</StartBoundary><Enabled>true</Enabled>
      <Repetition><Interval>PT{BACKSTOP_MIN}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{_xml(user)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowHardTerminate>true</AllowHardTerminate>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_xml(exe)}</Command>
    <Arguments>{_xml(subprocess.list2cmdline(args))}</Arguments></Exec></Actions>
</Task>"""


class TaskScheduler:
    """The one ARES wake task. Real implementation uses schtasks.exe; tests inject a fake."""

    def exists(self) -> bool:
        return si.IS_WINDOWS and si.run_hidden(["schtasks", "/Query", "/TN", TASK_NAME], timeout=15).ok

    def query_xml(self) -> str | None:
        if not si.IS_WINDOWS:
            return None
        res = si.run_hidden(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], timeout=15)
        return res.stdout if res.ok else None

    def register(self, next_run: datetime) -> tuple[bool, str]:
        if not si.IS_WINDOWS:
            return False, "Not running on Windows."
        exe, args = check_command()
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        tmp = Path(os.environ.get("TEMP", ".")) / f"ARES_wake_{os.getpid()}.xml"
        tmp.write_text(build_task_xml(exe, args, next_run, user), encoding="utf-16")
        try:
            # /F replaces the existing task of the same name: never a duplicate.
            res = si.run_hidden(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(tmp), "/F"], timeout=30)
        finally:
            tmp.unlink(missing_ok=True)
        return res.ok, (res.stdout or res.stderr).strip()

    def unregister(self) -> tuple[bool, str]:
        if not self.exists():
            return True, "No wake task."
        res = si.run_hidden(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=30)
        return res.ok, (res.stdout or res.stderr).strip()


SEPARATE_DATA_NOTE = ("Separate data folder (ARES_DATA_DIR): the Windows wake task was not changed "
                      "(it always checks the normal data folder).")


class SeparateDataScheduler(TaskScheduler):
    """Used when ARES runs on its own data folder (ARES_DATA_DIR: tests, portable copies).

    The wake task is one per Windows user, and its command carries no data folder, so it always
    checks the NORMAL data folder. A run on another folder must therefore never create, move or
    delete it (the same rule as for the Windows startup entry)."""

    def exists(self) -> bool:
        return False

    def query_xml(self) -> str | None:
        return None

    def register(self, next_run: datetime) -> tuple[bool, str]:
        return True, SEPARATE_DATA_NOTE

    def unregister(self) -> tuple[bool, str]:
        return True, SEPARATE_DATA_NOTE


def default_scheduler() -> TaskScheduler:
    """The real Task Scheduler, unless this run uses a separate data folder."""
    return SeparateDataScheduler() if os.environ.get("ARES_DATA_DIR") else TaskScheduler()


def task_facts(xml: str | None) -> dict:
    """Parse the registered task for the Test button: next run, wake flag, command."""
    if not xml:
        return {}
    from html import unescape

    def tag(name):
        m = re.search(fr"<{name}>(.*?)</{name}>", xml, re.S)
        return unescape(m.group(1).strip()) if m else None
    return {"next_run": tag("StartBoundary"), "wake_to_run": (tag("WakeToRun") or "").lower() == "true",
            "command": tag("Command"), "arguments": tag("Arguments")}


def remove_own_task(scheduler: TaskScheduler | None = None) -> tuple[bool, str]:
    """Uninstaller cleanup: remove the wake task only if it runs THIS copy of ARES. The saved
    Smart Wake-Up setting is not changed (a reinstall re-creates the task if it was on)."""
    scheduler = scheduler or TaskScheduler()
    try:
        facts = task_facts(scheduler.query_xml())
        if not facts:
            return True, "No wake task."
        exe, _ = check_command()
        if (facts.get("command") or "").strip('"').lower() != exe.lower():
            return True, "Wake task belongs to another ARES copy; left unchanged."
        ok, msg = scheduler.unregister()
        return ok, "Removed the wake task." if ok else f"Wake task not removed: {msg}"
    except Exception as exc:
        log.exception("remove_own_task failed")
        return False, f"Wake task cleanup failed: {exc}"


def ensure(db, scheduler: TaskScheduler | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """Make the wake task match the settings (register / refresh / remove). Never raises."""
    scheduler = scheduler or default_scheduler()
    now = now or datetime.now()
    try:
        settings = db.all_settings()
        if isinstance(scheduler, SeparateDataScheduler):
            return True, SEPARATE_DATA_NOTE
        if not settings.get("wake_enabled"):
            ok, msg = scheduler.unregister()
            return ok, "Smart Wake-Up off; wake task removed." if ok else f"Could not remove wake task: {msg}"
        ok, msg = scheduler.register(now + timedelta(minutes=base_interval(settings)))
        return ok, ("Wake check task scheduled." if ok else f"Could not create the wake task: {msg}")
    except Exception as exc:
        log.exception("Smart Wake-Up ensure failed")
        return False, f"Smart Wake-Up error: {exc}"


def test_configuration(db, scheduler: TaskScheduler | None = None,
                       support: WakeSupport | None = None) -> tuple[bool, list[str]]:
    """'Test wake configuration' (Settings): read-only report of whether wake checks can run.

    Checks the saved settings, the PC's sleep/wake support and the registered task (wake flag,
    command, next run). It never sleeps or wakes the PC, so it cannot prove that this hardware
    really wakes up: that needs the manual sleep test in the documentation. Never raises.
    """
    scheduler = scheduler or TaskScheduler()
    lines: list[str] = []
    ok = True
    try:
        s = db.all_settings()
        sup = support or detect()
        lines.append("Smart Wake-Up: " + ("ON" if s.get("wake_enabled") else "OFF"))
        lines.append(f"Checks every {base_interval(s)} min (slower when far from a threshold); "
                     f"low trigger {'<= ' + str(s.get('wake_low_threshold')) + '% on battery' if s.get('wake_low_enabled') else 'off'}, "
                     f"high trigger {'>= ' + str(s.get('wake_high_threshold')) + '% on charger' if s.get('wake_high_enabled') else 'off'}.")
        lines.append(sup.summary())
        problems = sup.problems()
        if not sup.windows:
            return False, lines + problems
        xml = scheduler.query_xml()
        facts = task_facts(xml)
        if s.get("wake_enabled"):
            if not facts:
                ok = False
                lines.append("✗ The wake task is not registered (ARES registers it a few seconds after "
                             "Smart Wake-Up is turned on and saved).")
            else:
                exe, args = check_command()
                lines.append(f"✓ Task '{TASK_NAME}' registered; next check {facts.get('next_run') or 'unknown'}.")
                if not facts.get("wake_to_run"):
                    ok = False
                    lines.append("✗ The task is not allowed to wake the computer.")
                if (facts.get("command") or "").strip('"').lower() != exe.lower():
                    ok = False
                    lines.append(f"✗ The task starts {facts.get('command')}, not this copy of ARES ({exe}).")
            if not sup.can_wake_from_sleep:
                ok = False
        elif facts:
            ok = False
            lines.append("✗ Smart Wake-Up is off but the wake task still exists (ARES removes it on its next check).")
        else:
            lines.append("No wake task registered (correct while Smart Wake-Up is off).")
        lines += ["⚠ " + p for p in problems]
        lines.append("This test does not put the PC to sleep. Real wake-up on this laptop is only proven by "
                     "the manual sleep test (see the documentation).")
        return ok, lines
    except Exception as exc:
        log.exception("Wake configuration test failed")
        return False, lines + [f"Test failed: {exc}"]


# --------------------------------------------------------------------------------------------
# The --wake-check process
# --------------------------------------------------------------------------------------------
class CheckLock:
    """Prevents overlapping check processes (Task Scheduler's IgnoreNew is the first guard)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                        self.path.unlink(missing_ok=True)       # left by a crashed check
                        continue
                except OSError:
                    pass
                return False
        return False

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


@dataclass
class CheckResult:
    ts: float
    percent: float | None
    ac: bool | None
    action: str | None
    result: str
    next_due: float | None
    late_min: int | None = None

    def text(self) -> str:
        when = datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M")
        pct = "N/A" if self.percent is None else f"{self.percent:.0f}%"
        ac = {True: "charger", False: "battery", None: "charger unknown"}[self.ac]
        late = f" (check ran {self.late_min} min late)" if self.late_min else ""
        return f"{when}: {pct} on {ac} – {self.result}{late}"


def run_check(db, probe: Callable[[], tuple | None], send: Callable[[bytes], bool],
              launch: Callable[[str], bool], scheduler: TaskScheduler | None = None,
              lock: CheckLock | None = None, clock: Callable[[], float] = time.time) -> CheckResult | None:
    """One wake check. Returns None if another check is already running."""
    scheduler = scheduler or default_scheduler()
    if lock is not None and not lock.acquire():
        log.info("Another wake check is running; exiting")
        return None
    try:
        now = clock()
        settings = db.all_settings()
        recent = [t for t in (db.get_state("wake_checks") or []) if now - t < 3600] + [now]
        db.set_state("wake_checks", recent)
        prev = db.get_state("wake_last_check") or {}
        late = None
        if prev.get("next_due") and now - prev["next_due"] > 2 * 60 * base_interval(settings):
            late = int((now - prev["next_due"]) // 60)
        reading = probe()
        pct, ac = reading if reading else (None, None)
        d = decide(pct, ac, settings, recent[:-1], now)
        result = d.reason
        if d.action:
            if send(f"wake:{d.action}".encode()):
                result += "; ARES (already running) was activated"
            elif launch(d.action):
                result += "; ARES was started"
            else:
                result += "; could not start ARES"
            if prev.get("action") != d.action:      # log the start of a trigger, not every repeat
                db.log_event("smart_wake", f"Wake check: {result}")
        if d.next_minutes is None:
            scheduler.unregister()
            next_due = None
        else:
            next_due = now + d.next_minutes * 60
            ok, msg = scheduler.register(datetime.fromtimestamp(next_due))
            if not ok:
                result += f"; could not schedule the next check ({msg})"
                db.log_event("warning", f"Smart Wake-Up: could not schedule the next check: {msg}")
        res = CheckResult(now, pct, ac, d.action, result, next_due, late)
        db.set_state("wake_last_check", asdict(res))
        return res
    finally:
        if lock is not None:
            lock.release()


def last_check_text(db) -> str:
    data = db.get_state("wake_last_check")
    if not data:
        return "No wake check has run yet."
    try:
        return CheckResult(**data).text()
    except TypeError:
        return "Unreadable last result."


def launch_ares(action: str) -> bool:
    """Start ARES (tray only) with the trigger; detached so the check process can exit."""
    from app.core import startup_manager

    exe, args = startup_manager.launch_command()
    args = [a for a in args if a != startup_manager.AUTOSTART_ARG] + ["--minimized", "--wake-trigger", action]
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([exe, *args], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        log.exception("Could not start ARES")
        return False
