"""
Disable_Update.py – backend for fully disabling / restoring Windows Update
on Windows 10/11.

What "disable" does (every step is reversed by "enable"):
  1. Backs up the original service and policy settings to
     HKLM\\SOFTWARE\\WUControl so they can be restored exactly.
  2. Applies Group Policy values: no automatic updates, and points the
     update client at a dead local WSUS server so scans cannot reach
     Microsoft even if a service gets started.
  3. Sets wuauserv, UsoSvc, WaaSMedicSvc, BITS and DoSvc to Disabled and
     removes their recovery actions (so nothing auto-restarts them).
     Protected keys (WaaSMedicSvc) are written using SeBackup/SeRestore
     privileges, which bypass the TrustedInstaller ACL.
  4. Stops the services.
  5. Disables the WindowsUpdate / UpdateOrchestrator / WaaSMedic scheduled
     tasks via a short-lived SYSTEM task (admins get Access Denied on them).
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import time
import uuid
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────
SERVICES: dict[str, str] = {
    "wuauserv":     "Windows Update",
    "UsoSvc":       "Update Orchestrator",
    "WaaSMedicSvc": "Windows Update Medic",
    "bits":         "Background Intelligent Transfer",
    "DoSvc":        "Delivery Optimization",
}

# Windows 10/11 defaults (Start, DelayedAutostart) – used only if no backup exists.
DEFAULT_START: dict[str, tuple[int, int]] = {
    "wuauserv":     (3, 0),
    "UsoSvc":       (2, 1),
    "WaaSMedicSvc": (3, 0),
    "bits":         (3, 0),
    "DoSvc":        (2, 1),
}

SERVICES_REG_BASE = r"SYSTEM\CurrentControlSet\Services"
WU_POLICY = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
AU_POLICY = WU_POLICY + r"\AU"
DEAD_WSUS = "http://127.0.0.1:8531"

# (key path, value name, reg type, value applied when disabling)
POLICY_VALUES: list[tuple[str, str, int, int | str]] = [
    (AU_POLICY, "NoAutoUpdate", winreg.REG_DWORD, 1),
    (AU_POLICY, "AUOptions",    winreg.REG_DWORD, 2),
    (AU_POLICY, "UseWUServer",  winreg.REG_DWORD, 1),
    (WU_POLICY, "WUServer",       winreg.REG_SZ, DEAD_WSUS),
    (WU_POLICY, "WUStatusServer", winreg.REG_SZ, DEAD_WSUS),
    (WU_POLICY, "DoNotConnectToWindowsUpdateInternetLocations", winreg.REG_DWORD, 1),
    (WU_POLICY, "ExcludeWUDriversInQualityUpdate", winreg.REG_DWORD, 1),
]

TASK_FOLDERS = [
    "\\Microsoft\\Windows\\WindowsUpdate\\",
    "\\Microsoft\\Windows\\UpdateOrchestrator\\",
    "\\Microsoft\\Windows\\WaaSMedic\\",
]

BACKUP_KEY = r"SOFTWARE\WUControl"
BACKUP_VALUE = "Backup"

CREATE_NO_WINDOW = 0x08000000

START_LABELS = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual", 4: "Disabled"}
STATE_LABELS = {1: "Stopped", 2: "Starting", 3: "Stopping", 4: "Running",
                5: "Resuming", 6: "Pausing", 7: "Paused"}

# ─────────────────────────────────────────────
#  Win32 bindings
# ─────────────────────────────────────────────
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_HKLM = ctypes.c_void_p(winreg.HKEY_LOCAL_MACHINE)
REG_OPTION_BACKUP_RESTORE = 0x4
KEY_ALL_ACCESS = 0xF003F
TOKEN_ADJUST_PRIVILEGES = 0x20
TOKEN_QUERY = 0x8
SE_PRIVILEGE_ENABLED = 0x2
SC_MANAGER_CONNECT = 0x1
SERVICE_QUERY_STATUS = 0x4


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD),
                ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


class _SERVICE_STATUS(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted",
        "dwWin32ExitCode", "dwServiceSpecificExitCode",
        "dwCheckPoint", "dwWaitHint")]


_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
_advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                            ctypes.POINTER(_LUID)]
_advapi32.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                            ctypes.POINTER(_TOKEN_PRIVILEGES),
                                            wintypes.DWORD, ctypes.c_void_p,
                                            ctypes.c_void_p]
_advapi32.RegCreateKeyExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                      wintypes.DWORD, wintypes.LPWSTR,
                                      wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(wintypes.DWORD)]
_advapi32.RegSetValueExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                     wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_char_p, wintypes.DWORD]
_advapi32.RegDeleteValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_advapi32.RegCloseKey.argtypes = [ctypes.c_void_p]
_advapi32.OpenSCManagerW.restype = ctypes.c_void_p
_advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_advapi32.OpenServiceW.restype = ctypes.c_void_p
_advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
_advapi32.QueryServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SERVICE_STATUS)]
_advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]


def _enable_privilege(name: str) -> bool:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(),
                                      TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                      ctypes.byref(token)):
        return False
    try:
        luid = _LUID()
        if not _advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = _TOKEN_PRIVILEGES(1, (_LUID_AND_ATTRIBUTES * 1)(
            _LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED)))
        ctypes.set_last_error(0)
        _advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0  # ERROR_NOT_ALL_ASSIGNED otherwise
    finally:
        _kernel32.CloseHandle(token)


def _encode(reg_type: int, value) -> bytes:
    if reg_type == winreg.REG_DWORD:
        return int(value).to_bytes(4, "little")
    if reg_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        return (str(value) + "\0").encode("utf-16-le")
    return bytes(value)


def _priv_reg(path: str, op: Callable[[ctypes.c_void_p], int]) -> bool:
    """Open an HKLM key with REG_OPTION_BACKUP_RESTORE (ignores ACLs) and run op."""
    hkey = ctypes.c_void_p()
    rc = _advapi32.RegCreateKeyExW(_HKLM, path, 0, None, REG_OPTION_BACKUP_RESTORE,
                                   KEY_ALL_ACCESS, None, ctypes.byref(hkey), None)
    if rc != 0:
        return False
    try:
        return op(hkey) in (0, 2)  # 2 = ERROR_FILE_NOT_FOUND on delete is fine
    finally:
        _advapi32.RegCloseKey(hkey)


# ─────────────────────────────────────────────
#  Registry helpers (normal access, privileged fallback)
# ─────────────────────────────────────────────
def reg_read(path: str, name: str):
    """Return (value, type) or (None, None) if missing."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, name)
    except OSError:
        return None, None


def reg_write(path: str, name: str, reg_type: int, value) -> bool:
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, reg_type, value)
        return True
    except PermissionError:
        data = _encode(reg_type, value)
        return _priv_reg(path, lambda h: _advapi32.RegSetValueExW(
            h, name, 0, reg_type, data, len(data)))
    except OSError:
        return False


def reg_delete(path: str, name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        return _priv_reg(path, lambda h: _advapi32.RegDeleteValueW(h, name))
    except OSError:
        return False


# ─────────────────────────────────────────────
#  Status model
# ─────────────────────────────────────────────
@dataclass
class ServiceInfo:
    name: str
    display: str
    start: int | None
    delayed: bool
    state: int | None

    @property
    def start_label(self) -> str:
        if self.start is None:
            return "Not installed"
        label = START_LABELS.get(self.start, f"Start={self.start}")
        return f"{label} (Delayed)" if self.start == 2 and self.delayed else label

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, "—") if self.state else "—"

    @property
    def disabled(self) -> bool:
        return self.start in (4, None)

    @property
    def running(self) -> bool:
        return self.state == 4


@dataclass
class Status:
    services: list[ServiceInfo]
    policy_applied: bool

    @property
    def overall(self) -> str:
        """'Disabled', 'Enabled' or 'Partial'."""
        disabled = [s.disabled for s in self.services if s.start is not None]
        if all(disabled) and self.policy_applied:
            return "Disabled"
        if not any(disabled) and not self.policy_applied:
            return "Enabled"
        return "Partial"


LogFn = Callable[[str, str], None]  # (level, message); level: info|ok|warn|err


class Disable_Update:
    """Disables / restores Windows Update on Windows 10/11."""

    def __init__(self, log: LogFn | None = None):
        self.log: LogFn = log or (lambda level, msg: print(f"[{level}] {msg}"))

    # ──────────────────────────────────────────
    #  Admin
    # ──────────────────────────────────────────
    @staticmethod
    def if_admin() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _require_admin(self) -> bool:
        if self.if_admin():
            _enable_privilege("SeBackupPrivilege")
            _enable_privilege("SeRestorePrivilege")
            return True
        self.log("err", "Administrator privileges are required.")
        return False

    # ──────────────────────────────────────────
    #  Service helpers
    # ──────────────────────────────────────────
    @staticmethod
    def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)

    @staticmethod
    def _svc_key(svc: str) -> str:
        return f"{SERVICES_REG_BASE}\\{svc}"

    def _get_start(self, svc: str) -> tuple[int | None, int]:
        start, _ = reg_read(self._svc_key(svc), "Start")
        delayed, _ = reg_read(self._svc_key(svc), "DelayedAutostart")
        return start, int(delayed or 0)

    @staticmethod
    def _get_state(svc: str) -> int | None:
        scm = _advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not scm:
            return None
        try:
            h = _advapi32.OpenServiceW(scm, svc, SERVICE_QUERY_STATUS)
            if not h:
                return None
            try:
                st = _SERVICE_STATUS()
                if _advapi32.QueryServiceStatus(h, ctypes.byref(st)):
                    return st.dwCurrentState
                return None
            finally:
                _advapi32.CloseServiceHandle(h)
        finally:
            _advapi32.CloseServiceHandle(scm)

    def _set_start(self, svc: str, start: int, delayed: int = 0) -> tuple[bool, bool]:
        """
        Set a service start type. Uses `sc config` first (takes effect
        immediately), falls back to a privileged registry write (needs reboot).
        Returns (success, reboot_required).
        """
        mode = {4: "disabled", 3: "demand",
                2: "delayed-auto" if delayed else "auto"}.get(start)
        if mode and self._run(["sc.exe", "config", svc, "start=", mode]).returncode == 0:
            return True, False
        key = self._svc_key(svc)
        ok = reg_write(key, "Start", winreg.REG_DWORD, start)
        if ok and start == 2:
            ok = reg_write(key, "DelayedAutostart", winreg.REG_DWORD, int(delayed))
        return ok, ok

    def _stop(self, svc: str, timeout: float = 20.0) -> bool:
        if self._get_state(svc) in (None, 1):
            return True
        self._run(["sc.exe", "stop", svc])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._get_state(svc) == 1:
                return True
            time.sleep(0.5)
        return False

    def _start(self, svc: str) -> bool:
        rc = self._run(["sc.exe", "start", svc]).returncode
        return rc in (0, 1056)  # 1056 = already running

    # ──────────────────────────────────────────
    #  Scheduled tasks (run as SYSTEM)
    # ──────────────────────────────────────────
    def _run_as_system(self, ps_body: str, timeout: float = 90.0) -> str | None:
        """
        Execute a PowerShell snippet as SYSTEM through a temporary scheduled
        task. The snippet must write its output to $ResultPath. Returns that
        output or None on failure/timeout.
        """
        temp_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")
        fd, script = tempfile.mkstemp(prefix="wuctl_", suffix=".ps1", dir=temp_dir)
        result = script[:-4] + ".out"
        task = f"WUControlHelper_{uuid.uuid4().hex[:8]}"
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                f.write(f"$ResultPath = '{result}'\n{ps_body}")
            cmd = (f"powershell.exe -NoProfile -NonInteractive "
                   f"-ExecutionPolicy Bypass -File {script}")
            created = self._run(["schtasks", "/Create", "/TN", task, "/TR", cmd,
                                 "/SC", "ONCE", "/ST", "00:00", "/RU", "SYSTEM",
                                 "/RL", "HIGHEST", "/F"])
            if created.returncode != 0:
                self.log("warn", f"Could not create SYSTEM helper task: "
                                 f"{created.stderr.strip()}")
                return None
            self._run(["schtasks", "/Run", "/TN", task])
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if os.path.exists(result):
                    with open(result, encoding="utf-8-sig", errors="replace") as f:
                        return f.read()
                time.sleep(0.5)
            self.log("warn", "SYSTEM helper task timed out.")
            return None
        finally:
            self._run(["schtasks", "/Delete", "/TN", task, "/F"])
            for p in (script, result):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _set_tasks(self, enable: bool) -> None:
        verb = "Enable" if enable else "Disable"
        folders = ",".join(f"'{p}'" for p in TASK_FOLDERS)
        stop_medic = "" if enable else (
            "Stop-Service -Name WaaSMedicSvc,UsoSvc,wuauserv -Force "
            "-ErrorAction SilentlyContinue\n")
        ps = f"""{stop_medic}$out = @()
foreach ($p in @({folders})) {{
  foreach ($t in @(Get-ScheduledTask -TaskPath $p -ErrorAction SilentlyContinue)) {{
    try {{ {verb}-ScheduledTask -InputObject $t -ErrorAction Stop | Out-Null
           $out += "OK|$($t.TaskPath)$($t.TaskName)" }}
    catch {{ $out += "ERR|$($t.TaskPath)$($t.TaskName)" }}
  }}
}}
$tmp = "$ResultPath.tmp"
$out | Set-Content -Path $tmp -Encoding UTF8
Move-Item -Path $tmp -Destination $ResultPath -Force
"""
        self.log("info", f"{verb[:-1]}ing update scheduled tasks (as SYSTEM)…")
        output = self._run_as_system(ps)
        if output is None:
            self.log("warn", "Scheduled tasks were not changed.")
            return
        lines = [l for l in output.splitlines() if "|" in l]
        ok = [l.split("|", 1)[1] for l in lines if l.startswith("OK|")]
        bad = [l.split("|", 1)[1] for l in lines if l.startswith("ERR|")]
        self.log("ok", f"{len(ok)} scheduled task(s) {verb.lower()}d.")
        for name in bad:
            self.log("warn", f"Could not {verb.lower()} task {name}")

    # ──────────────────────────────────────────
    #  Backup / restore
    # ──────────────────────────────────────────
    def _load_backup(self) -> dict | None:
        raw, _ = reg_read(BACKUP_KEY, BACKUP_VALUE)
        try:
            return json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def _save_backup(self) -> None:
        if self._load_backup():
            return  # keep the very first (pre-disable) snapshot
        services = {}
        for svc in SERVICES:
            start, delayed = self._get_start(svc)
            if start is None:
                continue
            if start == 4:  # already disabled before us – remember the default
                start, delayed = DEFAULT_START[svc]
            fa, _ = reg_read(self._svc_key(svc), "FailureActions")
            services[svc] = {"Start": start, "DelayedAutostart": delayed,
                             "FailureActions": fa.hex() if fa else None}
        policies = {}
        for path, name, _, disabled_value in POLICY_VALUES:
            value, rtype = reg_read(path, name)
            keep = isinstance(value, (int, str)) and value != disabled_value
            policies[f"{path}|{name}"] = [rtype, value] if keep else None
        data = json.dumps({"services": services, "policies": policies})
        if reg_write(BACKUP_KEY, BACKUP_VALUE, winreg.REG_SZ, data):
            self.log("ok", "Original settings backed up.")
        else:
            self.log("warn", "Could not save a backup; defaults will be used to restore.")

    # ──────────────────────────────────────────
    #  Status
    # ──────────────────────────────────────────
    def get_status(self) -> Status:
        infos = []
        for svc, display in SERVICES.items():
            start, delayed = self._get_start(svc)
            infos.append(ServiceInfo(svc, display, start, bool(delayed),
                                     self._get_state(svc)))
        no_auto, _ = reg_read(AU_POLICY, "NoAutoUpdate")
        use_wsus, _ = reg_read(AU_POLICY, "UseWUServer")
        server, _ = reg_read(WU_POLICY, "WUServer")
        policy = no_auto == 1 and use_wsus == 1 and server == DEAD_WSUS
        return Status(infos, policy)

    def is_disabled(self) -> bool:
        return self.get_status().overall == "Disabled"

    # ──────────────────────────────────────────
    #  Disable
    # ──────────────────────────────────────────
    def disable_update(self) -> bool:
        if not self._require_admin():
            return False
        self.log("info", "Disabling Windows Update…")
        self._save_backup()
        success, reboot = True, False

        for path, name, rtype, value in POLICY_VALUES:
            if not reg_write(path, name, rtype, value):
                self.log("err", f"Policy {name}: failed")
                success = False
        self.log("ok" if success else "warn", "Update policies applied.")

        # Disable first, then stop – so triggers can't restart the service.
        for svc, display in SERVICES.items():
            if self._get_start(svc)[0] is None:
                self.log("info", f"{display} ({svc}) not present – skipped.")
                continue
            ok, needs_reboot = self._set_start(svc, 4)
            reboot |= needs_reboot
            reg_delete(self._svc_key(svc), "FailureActions")
            if not ok:
                self.log("err", f"{display}: could not disable")
                success = False
                continue
            stopped = self._stop(svc)
            self.log("ok" if stopped else "warn",
                     f"{display}: disabled" + ("" if stopped else
                                                " (still running until reboot)"))
            reboot |= not stopped

        self._set_tasks(enable=False)

        if success:
            self.log("ok", "Windows Update is DISABLED.")
        else:
            self.log("err", "Some steps failed – see messages above.")
        if reboot:
            self.log("warn", "Reboot required for all changes to take effect.")
        return success

    # ──────────────────────────────────────────
    #  Enable
    # ──────────────────────────────────────────
    def enable_update(self) -> bool:
        if not self._require_admin():
            return False
        self.log("info", "Restoring Windows Update…")
        backup = self._load_backup()
        if backup is None:
            self.log("warn", "No backup found – restoring Windows defaults.")
            backup = {"services": {}, "policies": {}}
        success, reboot = True, False

        for path, name, _, _ in POLICY_VALUES:
            saved = backup["policies"].get(f"{path}|{name}")
            ok = reg_write(path, name, saved[0], saved[1]) if saved else reg_delete(path, name)
            if not ok:
                self.log("err", f"Policy {name}: could not restore")
                success = False
        self.log("ok" if success else "warn", "Update policies restored.")

        for svc, display in SERVICES.items():
            if self._get_start(svc)[0] is None:
                continue
            saved = backup["services"].get(svc, {})
            start = saved.get("Start", DEFAULT_START[svc][0])
            delayed = saved.get("DelayedAutostart", DEFAULT_START[svc][1])
            ok, needs_reboot = self._set_start(svc, start, delayed)
            reboot |= needs_reboot
            if saved.get("FailureActions"):
                reg_write(self._svc_key(svc), "FailureActions", winreg.REG_BINARY,
                          bytes.fromhex(saved["FailureActions"]))
            if not ok:
                self.log("err", f"{display}: could not restore start type")
                success = False
                continue
            msg = f"{display}: {START_LABELS.get(start, start)}"
            if start == 2 and not needs_reboot:
                msg += ", started" if self._start(svc) else ", will start on boot"
            self.log("ok", msg)

        self._set_tasks(enable=True)

        if success:
            reg_delete(BACKUP_KEY, BACKUP_VALUE)
            self.log("ok", "Windows Update is ENABLED.")
        else:
            self.log("err", "Some steps failed – see messages above.")
        if reboot:
            self.log("warn", "Reboot required for all changes to take effect.")
        return success
