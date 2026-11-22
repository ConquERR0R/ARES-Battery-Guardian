"""Low-level Windows system access used by the rest of the application.

Everything here is a thin wrapper around documented Windows APIs:

* GetSystemPowerStatus (kernel32)          - always-available battery summary
* IOCTL_BATTERY_QUERY_* on the battery device (battery class driver)
                                           - detailed telemetry (mWh, mW, mV, temp)
* Registry HKLM\\HARDWARE\\DESCRIPTION\\System\\BIOS - manufacturer / model / BIOS
* PowerShell Get-CimInstance              - WMI fallback for static data
* shell32.IsUserAnAdmin / ShellExecuteW    - elevation checks / relaunch

Nothing in this module writes to hardware. On non-Windows platforms every
function returns "unavailable" results instead of raising, so the GUI and the
test-suite can run anywhere while never inventing values.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #
@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_hidden(args: list[str], timeout: float = 30.0, input_text: str | None = None) -> CommandResult:
    """Run a process without flashing a console window. Never raises."""
    kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(-1, str(exc.stdout or ""), "timeout", timed_out=True)
    except (OSError, ValueError) as exc:
        return CommandResult(-1, "", str(exc))


def powershell(script: str, timeout: float = 30.0) -> CommandResult:
    """Run a PowerShell script passed on stdin (so secrets never hit the command line)."""
    return run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
        timeout=timeout,
        input_text=script,
    )


def cim_query(class_name: str, namespace: str = "root/cimv2", properties: list[str] | None = None,
              where: str | None = None, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Query WMI via PowerShell and return a list of dicts. Empty list on any failure."""
    if not IS_WINDOWS:
        return []
    props = ",".join(properties) if properties else "*"
    filt = f" -Filter \"{where}\"" if where else ""
    script = (
        "$ErrorActionPreference='Stop';"
        f"$r=@(Get-CimInstance -Namespace '{namespace}' -ClassName '{class_name}'{filt} | "
        f"Select-Object {props});"
        "ConvertTo-Json -InputObject $r -Depth 3 -Compress"
    )
    res = powershell(script, timeout=timeout)
    if not res.ok or not res.stdout.strip():
        log.debug("CIM query %s/%s failed: %s", namespace, class_name, res.stderr.strip()[:300])
        return []
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)]


# --------------------------------------------------------------------------- #
# Elevation
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(extra_args: list[str] | None = None) -> bool:
    """Ask Windows (UAC) to start a new elevated copy of this app. Returns True if launched."""
    if not IS_WINDOWS:
        return False
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = list(sys.argv[1:])
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        params = [os.path.abspath(sys.argv[0])] + list(sys.argv[1:])
    params += extra_args or []
    param_str = subprocess.list2cmdline(params)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, param_str, None, 1)
    return rc > 32


# --------------------------------------------------------------------------- #
# Machine identity (registry, no WMI cost)
# --------------------------------------------------------------------------- #
@dataclass
class MachineInfo:
    manufacturer: str | None = None
    model: str | None = None
    bios_vendor: str | None = None
    bios_version: str | None = None
    bios_date: str | None = None
    os_name: str | None = None
    os_build: str | None = None


def get_machine_info() -> MachineInfo:
    info = MachineInfo()
    if not IS_WINDOWS:
        return info
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as k:
            def rv(name: str) -> str | None:
                try:
                    v = winreg.QueryValueEx(k, name)[0]
                    return str(v).strip() or None
                except OSError:
                    return None

            info.manufacturer = rv("SystemManufacturer")
            info.model = rv("SystemProductName")
            info.bios_vendor = rv("BIOSVendor")
            info.bios_version = rv("BIOSVersion")
            info.bios_date = rv("BIOSReleaseDate")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            try:
                info.os_name = str(winreg.QueryValueEx(k, "ProductName")[0])
            except OSError:
                pass
            try:
                build = int(winreg.QueryValueEx(k, "CurrentBuildNumber")[0])
                info.os_build = str(build)
                # ProductName still says "Windows 10" on Windows 11; builds >= 22000 are Windows 11.
                if build >= 22000 and info.os_name:
                    info.os_name = info.os_name.replace("Windows 10", "Windows 11")
            except (OSError, ValueError):
                pass
    except OSError as exc:
        log.warning("Could not read machine info from registry: %s", exc)
    if not info.manufacturer:
        rows = cim_query("Win32_ComputerSystem", properties=["Manufacturer", "Model"])
        if rows:
            info.manufacturer = rows[0].get("Manufacturer")
            info.model = rows[0].get("Model")
    return info


# --------------------------------------------------------------------------- #
# GetSystemPowerStatus
# --------------------------------------------------------------------------- #
class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


@dataclass
class PowerStatus:
    ac_online: bool | None
    percent: int | None
    charging: bool | None
    no_battery: bool
    battery_saver_on: bool | None
    seconds_remaining: int | None


def get_power_status() -> PowerStatus | None:
    if not IS_WINDOWS:
        return None
    s = _SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return None
    ac = {0: False, 1: True}.get(s.ACLineStatus)
    flag = s.BatteryFlag
    unknown_flag = flag == 255
    no_batt = (flag & 128) != 0 and not unknown_flag
    pct = None if s.BatteryLifePercent == 255 else int(s.BatteryLifePercent)
    charging = None if unknown_flag else bool(flag & 8)
    secs = None if s.BatteryLifeTime == 0xFFFFFFFF else int(s.BatteryLifeTime)
    return PowerStatus(ac, pct, charging, no_batt, bool(s.SystemStatusFlag & 1), secs)


# --------------------------------------------------------------------------- #
# Battery class driver IOCTLs (read-only)
# --------------------------------------------------------------------------- #
IOCTL_BATTERY_QUERY_TAG = 0x294040
IOCTL_BATTERY_QUERY_INFORMATION = 0x294044
IOCTL_BATTERY_QUERY_STATUS = 0x29404C

BATTERY_POWER_ON_LINE = 0x00000001
BATTERY_DISCHARGING = 0x00000002
BATTERY_CHARGING = 0x00000004
BATTERY_CRITICAL = 0x00000008
BATTERY_CAPACITY_RELATIVE = 0x40000000
BATTERY_UNKNOWN_CAPACITY = 0xFFFFFFFF
BATTERY_UNKNOWN_VOLTAGE = 0xFFFFFFFF
BATTERY_UNKNOWN_RATE = -0x80000000

# BATTERY_QUERY_INFORMATION_LEVEL
LEVEL_INFORMATION = 0
LEVEL_TEMPERATURE = 2
LEVEL_DEVICE_NAME = 4
LEVEL_MANUFACTURE_DATE = 5
LEVEL_MANUFACTURE_NAME = 6
LEVEL_SERIAL_NUMBER = 8


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


# GUID_DEVCLASS_BATTERY {72631E54-78A4-11D0-BCF7-00AA00B7B32A}
_GUID_DEVCLASS_BATTERY = _GUID(0x72631E54, 0x78A4, 0x11D0,
                               (ctypes.c_ubyte * 8)(0xBC, 0xF7, 0x00, 0xAA, 0x00, 0xB7, 0xB3, 0x2A))


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("InterfaceClassGuid", _GUID),
                ("Flags", ctypes.c_ulong), ("Reserved", ctypes.c_void_p)]


class _BATTERY_QUERY_INFORMATION(ctypes.Structure):
    _fields_ = [("BatteryTag", ctypes.c_ulong), ("InformationLevel", ctypes.c_int),
                ("AtRate", ctypes.c_long)]


class _BATTERY_INFORMATION(ctypes.Structure):
    _fields_ = [("Capabilities", ctypes.c_ulong), ("Technology", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte * 3), ("Chemistry", ctypes.c_ubyte * 4),
                ("DesignedCapacity", ctypes.c_ulong), ("FullChargedCapacity", ctypes.c_ulong),
                ("DefaultAlert1", ctypes.c_ulong), ("DefaultAlert2", ctypes.c_ulong),
                ("CriticalBias", ctypes.c_ulong), ("CycleCount", ctypes.c_ulong)]


class _BATTERY_WAIT_STATUS(ctypes.Structure):
    _fields_ = [("BatteryTag", ctypes.c_ulong), ("Timeout", ctypes.c_ulong),
                ("PowerState", ctypes.c_ulong), ("LowCapacity", ctypes.c_ulong),
                ("HighCapacity", ctypes.c_ulong)]


class _BATTERY_STATUS(ctypes.Structure):
    _fields_ = [("PowerState", ctypes.c_ulong), ("Capacity", ctypes.c_ulong),
                ("Voltage", ctypes.c_ulong), ("Rate", ctypes.c_long)]


class _BATTERY_MANUFACTURE_DATE(ctypes.Structure):
    _fields_ = [("Day", ctypes.c_ubyte), ("Month", ctypes.c_ubyte), ("Year", ctypes.c_ushort)]


@dataclass
class BatteryIoctlData:
    device_path: str
    power_state: int | None = None
    capacity_mwh: int | None = None
    voltage_mv: int | None = None
    rate_mw: int | None = None
    designed_capacity_mwh: int | None = None
    full_charged_capacity_mwh: int | None = None
    cycle_count: int | None = None
    chemistry: str | None = None
    relative_units: bool = False
    temperature_c: float | None = None
    device_name: str | None = None
    manufacturer: str | None = None
    serial_number: str | None = None
    manufacture_date: str | None = None
    errors: list[str] = field(default_factory=list)


def _setup_win32_prototypes():
    from ctypes import wintypes

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                                     wintypes.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
                                                          ctypes.c_void_p, wintypes.DWORD,
                                                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                         ctypes.c_void_p]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return setupapi, kernel32


_PROTOS = None


def _protos():
    global _PROTOS
    if _PROTOS is None:
        _PROTOS = _setup_win32_prototypes()
    return _PROTOS


def list_battery_device_paths() -> list[str]:
    if not IS_WINDOWS:
        return []
    from ctypes import wintypes

    setupapi, _ = _protos()
    DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(_GUID_DEVCLASS_BATTERY), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == ctypes.c_void_p(-1).value:
        return []
    paths: list[str] = []
    try:
        for idx in range(8):
            did = _SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(_GUID_DEVCLASS_BATTERY),
                                                        idx, ctypes.byref(did)):
                break
            required = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0,
                                                      ctypes.byref(required), None)
            if required.value == 0:
                continue
            buf = ctypes.create_string_buffer(required.value)
            # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W: 8 on 64-bit, 6 on 32-bit.
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf, required,
                                                         None, None):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                if path:
                    paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return paths


def _ioctl(kernel32, handle, code, in_obj, out_obj) -> int:
    """Returns 0 on success, otherwise the Win32 error code."""
    from ctypes import wintypes

    returned = wintypes.DWORD(0)
    in_ptr = ctypes.byref(in_obj) if in_obj is not None else None
    in_size = ctypes.sizeof(in_obj) if in_obj is not None else 0
    ok = kernel32.DeviceIoControl(handle, code, in_ptr, in_size, ctypes.byref(out_obj),
                                  ctypes.sizeof(out_obj), ctypes.byref(returned), None)
    return 0 if ok else ctypes.get_last_error()


def _query_string(kernel32, handle, tag: int, level: int) -> str | None:
    q = _BATTERY_QUERY_INFORMATION(tag, level, 0)
    buf = (ctypes.c_wchar * 128)()
    if _ioctl(kernel32, handle, IOCTL_BATTERY_QUERY_INFORMATION, q, buf) != 0:
        return None
    text = ctypes.wstring_at(ctypes.addressof(buf)).strip()
    return text or None


def read_battery_ioctl(device_path: str) -> BatteryIoctlData | None:
    """Read one battery's telemetry via the Windows battery class driver."""
    if not IS_WINDOWS:
        return None
    from ctypes import wintypes

    _, kernel32 = _protos()
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 3, 0x80
    handle = kernel32.CreateFileW(device_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        log.debug("CreateFile on battery failed: %s", ctypes.get_last_error())
        return None
    out = BatteryIoctlData(device_path=device_path)
    try:
        wait = wintypes.DWORD(0)
        tag = wintypes.DWORD(0)
        err = _ioctl(kernel32, handle, IOCTL_BATTERY_QUERY_TAG, wait, tag)
        if err != 0 or tag.value == 0:
            out.errors.append(f"QUERY_TAG failed ({err})")
            return out

        bi = _BATTERY_INFORMATION()
        err = _ioctl(kernel32, handle, IOCTL_BATTERY_QUERY_INFORMATION,
                     _BATTERY_QUERY_INFORMATION(tag.value, LEVEL_INFORMATION, 0), bi)
        if err == 0:
            out.relative_units = bool(bi.Capabilities & BATTERY_CAPACITY_RELATIVE)
            chem = bytes(bi.Chemistry).decode("ascii", "ignore").strip("\x00 ").strip()
            out.chemistry = chem or None

            def cap(v: int) -> int | None:
                return None if v in (0, BATTERY_UNKNOWN_CAPACITY) else int(v)

            out.designed_capacity_mwh = cap(bi.DesignedCapacity)
            out.full_charged_capacity_mwh = cap(bi.FullChargedCapacity)
            out.cycle_count = int(bi.CycleCount) if bi.CycleCount not in (0, 0xFFFFFFFF) else None
        else:
            out.errors.append(f"BatteryInformation failed ({err})")

        ws = _BATTERY_WAIT_STATUS(tag.value, 0, 0, 0, 0)
        bs = _BATTERY_STATUS()
        err = _ioctl(kernel32, handle, IOCTL_BATTERY_QUERY_STATUS, ws, bs)
        if err == 0:
            out.power_state = int(bs.PowerState)
            out.capacity_mwh = None if bs.Capacity == BATTERY_UNKNOWN_CAPACITY else int(bs.Capacity)
            out.voltage_mv = None if bs.Voltage in (0, BATTERY_UNKNOWN_VOLTAGE) else int(bs.Voltage)
            out.rate_mw = None if bs.Rate == BATTERY_UNKNOWN_RATE else int(bs.Rate)
        else:
            out.errors.append(f"QUERY_STATUS failed ({err})")

        # Temperature: many batteries/drivers do not implement it. Error => N/A, never guessed.
        temp = wintypes.ULONG(0)
        err = _ioctl(kernel32, handle, IOCTL_BATTERY_QUERY_INFORMATION,
                     _BATTERY_QUERY_INFORMATION(tag.value, LEVEL_TEMPERATURE, 0), temp)
        if err == 0 and temp.value > 0:
            out.temperature_c = round(temp.value / 10.0 - 273.15, 1)  # tenths of Kelvin

        out.device_name = _query_string(kernel32, handle, tag.value, LEVEL_DEVICE_NAME)
        out.manufacturer = _query_string(kernel32, handle, tag.value, LEVEL_MANUFACTURE_NAME)
        out.serial_number = _query_string(kernel32, handle, tag.value, LEVEL_SERIAL_NUMBER)
        md = _BATTERY_MANUFACTURE_DATE()
        if _ioctl(kernel32, handle, IOCTL_BATTERY_QUERY_INFORMATION,
                  _BATTERY_QUERY_INFORMATION(tag.value, LEVEL_MANUFACTURE_DATE, 0), md) == 0 and md.Year > 1990:
            out.manufacture_date = f"{md.Year:04d}-{md.Month:02d}-{md.Day:02d}"
    finally:
        kernel32.CloseHandle(handle)
    return out


def read_wmi_battery_static() -> dict[str, Any]:
    """WMI fallback (root\\wmi) for static battery data. Slow (spawns PowerShell); call rarely."""
    result: dict[str, Any] = {}
    rows = cim_query("BatteryStaticData", "root/wmi",
                     ["DesignedCapacity", "ManufactureName", "DeviceName", "SerialNumber", "Chemistry"])
    if rows:
        r = rows[0]
        result["designed_capacity_mwh"] = r.get("DesignedCapacity") or None
        result["manufacturer"] = (r.get("ManufactureName") or "").strip() or None
        result["device_name"] = (r.get("DeviceName") or "").strip() or None
        result["serial_number"] = (str(r.get("SerialNumber") or "")).strip() or None
    rows = cim_query("BatteryFullChargedCapacity", "root/wmi", ["FullChargedCapacity"])
    if rows:
        result["full_charged_capacity_mwh"] = rows[0].get("FullChargedCapacity") or None
    rows = cim_query("BatteryCycleCount", "root/wmi", ["CycleCount"])
    if rows:
        result["cycle_count"] = rows[0].get("CycleCount") or None
    return result
