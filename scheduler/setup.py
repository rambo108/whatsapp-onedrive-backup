"""Windows Task Scheduler integration for the WhatsApp OneDrive backup tool.

This module provides a :class:`SchedulerSetup` helper that:

* Installs / removes a recurring scheduled task that runs ``python main.py backup``
  via the Windows Task Scheduler (``schtasks.exe``) using a generated XML
  definition. The task runs only when the current user is logged on, with
  highest available privileges, and uses a hidden console window.
* Manages an iTunes autostart entry (Startup-folder shortcut, with a registry
  Run-key fallback) so iTunes is launched automatically at login. iTunes is
  required to be running for the backup tool to access the WhatsApp database
  inside the device backup.

Only standard library modules are used.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TASK_NAME = "WhatsAppOneDriveBackup"
TASK_AUTHOR = "WhatsAppOneDriveBackup"
TASK_DESCRIPTION = (
    "Periodically backs up WhatsApp data from the latest iTunes device backup "
    "to OneDrive."
)

ITUNES_RUN_VALUE_NAME = "WhatsAppOneDriveBackup_iTunesAutostart"
ITUNES_SHORTCUT_NAME = "iTunes (WhatsAppOneDriveBackup).lnk"

# Standard Windows Task Scheduler XML namespace.
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _run(
    cmd: list[str],
    *,
    check: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command without popping a console window."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    logger.debug("Running command: %s", cmd)
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        creationflags=creationflags,
    )


class SchedulerSetup:
    """Manage the scheduled backup task and iTunes autostart entry."""

    def __init__(
        self,
        task_name: str = TASK_NAME,
        project_root: Path | None = None,
    ) -> None:
        self.task_name = task_name
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parent.parent
        )

    # ------------------------------------------------------------------ #
    # Backup task
    # ------------------------------------------------------------------ #
    def install_backup_task(
        self,
        interval_minutes: int = 60,
        python_exe: str | None = None,
        script_path: str | None = None,
    ) -> None:
        """Register the recurring backup task with Windows Task Scheduler.

        Parameters
        ----------
        interval_minutes:
            Repetition interval, in minutes. Must be a positive integer.
        python_exe:
            Path to the Python interpreter to execute. Defaults to
            :data:`sys.executable` (preferring ``pythonw.exe`` if present so
            no console flashes).
        script_path:
            Path to ``main.py``. Defaults to ``<project_root>/main.py``.
        """
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")

        python_exe = python_exe or self._default_python_exe()
        script_path = script_path or str(self.project_root / "main.py")

        if not Path(python_exe).exists():
            raise FileNotFoundError(f"Python interpreter not found: {python_exe}")
        if not Path(script_path).exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        xml = self._build_task_xml(
            interval_minutes=interval_minutes,
            python_exe=python_exe,
            script_path=script_path,
            working_dir=str(self.project_root),
        )

        # schtasks /Create requires the XML on disk in UTF-16 LE w/ BOM.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".xml",
            delete=False,
            encoding="utf-16",
        )
        try:
            tmp.write(xml)
            tmp.close()

            cmd = [
                "schtasks",
                "/Create",
                "/TN",
                self.task_name,
                "/XML",
                tmp.name,
                "/F",
            ]
            result = _run(cmd)
            if result.returncode != 0:
                raise RuntimeError(
                    f"schtasks /Create failed (exit {result.returncode}): "
                    f"{(result.stderr or result.stdout or '').strip()}"
                )
            logger.info(
                "Installed scheduled task '%s' (every %d minute(s)).",
                self.task_name,
                interval_minutes,
            )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                logger.debug("Could not remove temp XML %s", tmp.name)

    def uninstall_backup_task(self) -> None:
        """Remove the scheduled backup task if present."""
        if not self.is_task_installed():
            logger.info("Task '%s' is not installed; nothing to remove.", self.task_name)
            return

        result = _run(["schtasks", "/Delete", "/TN", self.task_name, "/F"])
        if result.returncode != 0:
            raise RuntimeError(
                f"schtasks /Delete failed (exit {result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()}"
            )
        logger.info("Removed scheduled task '%s'.", self.task_name)

    def is_task_installed(self) -> bool:
        """Return ``True`` if the scheduled task exists."""
        result = _run(["schtasks", "/Query", "/TN", self.task_name])
        return result.returncode == 0

    def get_task_status(self) -> dict:
        """Return last/next run time and last result for the task.

        Keys: ``installed``, ``status``, ``last_run_time``, ``next_run_time``,
        ``last_result``. Missing fields are ``None``.
        """
        info: dict = {
            "installed": False,
            "status": None,
            "last_run_time": None,
            "next_run_time": None,
            "last_result": None,
        }

        result = _run(
            ["schtasks", "/Query", "/TN", self.task_name, "/V", "/FO", "LIST"]
        )
        if result.returncode != 0:
            return info

        info["installed"] = True
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if not value:
                continue
            if key == "status":
                info["status"] = value
            elif key == "last run time":
                info["last_run_time"] = self._parse_schtasks_time(value)
            elif key == "next run time":
                info["next_run_time"] = self._parse_schtasks_time(value)
            elif key == "last result":
                try:
                    info["last_result"] = int(value, 0)
                except ValueError:
                    info["last_result"] = value
        return info

    # ------------------------------------------------------------------ #
    # iTunes autostart
    # ------------------------------------------------------------------ #
    def find_itunes_path(self) -> Path | None:
        """Locate ``iTunes.exe`` on the current machine."""
        candidates: list[Path] = [
            Path(r"C:\Program Files\iTunes\iTunes.exe"),
            Path(r"C:\Program Files (x86)\iTunes\iTunes.exe"),
        ]

        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        for base in (program_files, program_files_x86):
            if base:
                candidates.append(Path(base) / "iTunes" / "iTunes.exe")

        for candidate in candidates:
            if candidate.is_file():
                logger.debug("Found iTunes at %s", candidate)
                return candidate

        # Microsoft Store install: query the App Paths registry key.
        store_path = self._query_app_paths("iTunes.exe")
        if store_path and Path(store_path).is_file():
            logger.debug("Found iTunes (Store) at %s", store_path)
            return Path(store_path)

        logger.warning("iTunes executable not found.")
        return None

    def enable_itunes_autostart(self) -> None:
        """Configure iTunes to launch automatically at user login."""
        itunes_path = self.find_itunes_path()
        if itunes_path is None:
            raise FileNotFoundError(
                "Could not locate iTunes.exe. Install iTunes from apple.com or "
                "the Microsoft Store and try again."
            )

        startup_dir = self._startup_folder()
        startup_dir.mkdir(parents=True, exist_ok=True)
        shortcut_path = startup_dir / ITUNES_SHORTCUT_NAME

        try:
            self._create_shortcut(shortcut_path, itunes_path)
            logger.info("Created iTunes startup shortcut at %s", shortcut_path)
            return
        except Exception as exc:  # pragma: no cover - fallback path
            logger.warning(
                "Failed to create startup shortcut (%s); falling back to "
                "registry Run key.",
                exc,
            )

        self._set_run_key(itunes_path)
        logger.info(
            "Registered iTunes autostart via HKCU Run key (%s).",
            ITUNES_RUN_VALUE_NAME,
        )

    def disable_itunes_autostart(self) -> None:
        """Remove the iTunes autostart entry (shortcut and/or Run key)."""
        removed_any = False

        shortcut_path = self._startup_folder() / ITUNES_SHORTCUT_NAME
        if shortcut_path.exists():
            try:
                shortcut_path.unlink()
                removed_any = True
                logger.info("Removed iTunes startup shortcut %s", shortcut_path)
            except OSError as exc:
                logger.error("Could not delete %s: %s", shortcut_path, exc)

        if self._delete_run_key():
            removed_any = True
            logger.info("Removed iTunes Run key value '%s'.", ITUNES_RUN_VALUE_NAME)

        if not removed_any:
            logger.info("No iTunes autostart entry was present.")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _default_python_exe() -> str:
        """Pick a sensible default Python interpreter.

        Prefers ``pythonw.exe`` next to ``python.exe`` so no console window
        flashes on each run.
        """
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
        return str(exe)

    def _build_task_xml(
        self,
        *,
        interval_minutes: int,
        python_exe: str,
        script_path: str,
        working_dir: str,
    ) -> str:
        """Construct the Task Scheduler XML definition."""
        ET.register_namespace("", _TASK_NS)
        ns = f"{{{_TASK_NS}}}"

        task = ET.Element(f"{ns}Task", attrib={"version": "1.4"})

        # ---------------- RegistrationInfo ----------------
        reg = ET.SubElement(task, f"{ns}RegistrationInfo")
        ET.SubElement(reg, f"{ns}Date").text = datetime.now().isoformat(timespec="seconds")
        ET.SubElement(reg, f"{ns}Author").text = TASK_AUTHOR
        ET.SubElement(reg, f"{ns}Description").text = TASK_DESCRIPTION
        ET.SubElement(reg, f"{ns}URI").text = f"\\{self.task_name}"

        # ---------------- Triggers ----------------
        triggers = ET.SubElement(task, f"{ns}Triggers")
        time_trigger = ET.SubElement(triggers, f"{ns}TimeTrigger")
        # Start one minute from now so the trigger is in the future.
        start = datetime.now().replace(microsecond=0)
        ET.SubElement(time_trigger, f"{ns}StartBoundary").text = start.isoformat()
        ET.SubElement(time_trigger, f"{ns}Enabled").text = "true"

        repetition = ET.SubElement(time_trigger, f"{ns}Repetition")
        ET.SubElement(repetition, f"{ns}Interval").text = f"PT{interval_minutes}M"
        ET.SubElement(repetition, f"{ns}Duration").text = "P9999D"
        ET.SubElement(repetition, f"{ns}StopAtDurationEnd").text = "false"

        # ---------------- Principals ----------------
        principals = ET.SubElement(task, f"{ns}Principals")
        principal = ET.SubElement(principals, f"{ns}Principal", attrib={"id": "Author"})
        ET.SubElement(principal, f"{ns}UserId").text = self._current_user_sid_or_name()
        ET.SubElement(principal, f"{ns}LogonType").text = "InteractiveToken"
        ET.SubElement(principal, f"{ns}RunLevel").text = "HighestAvailable"

        # ---------------- Settings ----------------
        settings = ET.SubElement(task, f"{ns}Settings")
        ET.SubElement(settings, f"{ns}MultipleInstancesPolicy").text = "IgnoreNew"
        ET.SubElement(settings, f"{ns}DisallowStartIfOnBatteries").text = "false"
        ET.SubElement(settings, f"{ns}StopIfGoingOnBatteries").text = "false"
        ET.SubElement(settings, f"{ns}AllowHardTerminate").text = "true"
        ET.SubElement(settings, f"{ns}StartWhenAvailable").text = "true"
        ET.SubElement(settings, f"{ns}RunOnlyIfNetworkAvailable").text = "false"
        ET.SubElement(settings, f"{ns}AllowStartOnDemand").text = "true"
        ET.SubElement(settings, f"{ns}Enabled").text = "true"
        ET.SubElement(settings, f"{ns}Hidden").text = "true"
        ET.SubElement(settings, f"{ns}RunOnlyIfIdle").text = "false"
        ET.SubElement(settings, f"{ns}WakeToRun").text = "false"
        ET.SubElement(settings, f"{ns}ExecutionTimeLimit").text = "PT1H"
        ET.SubElement(settings, f"{ns}Priority").text = "7"

        idle_settings = ET.SubElement(settings, f"{ns}IdleSettings")
        ET.SubElement(idle_settings, f"{ns}StopOnIdleEnd").text = "true"
        ET.SubElement(idle_settings, f"{ns}RestartOnIdle").text = "false"

        # ---------------- Actions ----------------
        actions = ET.SubElement(task, f"{ns}Actions", attrib={"Context": "Author"})
        exec_action = ET.SubElement(actions, f"{ns}Exec")
        ET.SubElement(exec_action, f"{ns}Command").text = python_exe
        ET.SubElement(exec_action, f"{ns}Arguments").text = (
            f'"{script_path}" backup'
        )
        ET.SubElement(exec_action, f"{ns}WorkingDirectory").text = working_dir

        # Serialize with XML declaration. schtasks accepts UTF-16 with BOM.
        body = ET.tostring(task, encoding="unicode")
        return '<?xml version="1.0" encoding="UTF-16"?>\n' + body

    @staticmethod
    def _current_user_sid_or_name() -> str:
        """Return the current user's identity for the task Principal."""
        domain = os.environ.get("USERDOMAIN")
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if domain and user:
            return f"{domain}\\{user}"
        return user

    @staticmethod
    def _parse_schtasks_time(value: str) -> datetime | str:
        """Parse a date/time string emitted by ``schtasks /Query /V``."""
        if not value or value.lower().startswith("n/a") or "never" in value.lower():
            return value
        for fmt in (
            "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return value

    @staticmethod
    def _startup_folder() -> Path:
        """Return ``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup``."""
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable is not set.")
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    @staticmethod
    def _create_shortcut(shortcut_path: Path, target: Path) -> None:
        """Create a .lnk shortcut to ``target`` using PowerShell + WScript.Shell."""
        ps = (
            "$ErrorActionPreference = 'Stop'; "
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut('{shortcut_path}'); "
            f"$s.TargetPath = '{target}'; "
            f"$s.WorkingDirectory = '{target.parent}'; "
            "$s.WindowStyle = 7; "  # 7 = minimized
            "$s.Description = 'Launches iTunes for WhatsApp OneDrive Backup'; "
            "$s.Save()"
        )
        result = _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"PowerShell shortcut creation failed: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )

    @staticmethod
    def _set_run_key(target: Path) -> None:
        """Add an HKCU Run-key value pointing at ``target``."""
        cmd = [
            "reg",
            "add",
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            "/v",
            ITUNES_RUN_VALUE_NAME,
            "/t",
            "REG_SZ",
            "/d",
            f'"{target}"',
            "/f",
        ]
        result = _run(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"reg add failed: {(result.stderr or result.stdout or '').strip()}"
            )

    @staticmethod
    def _delete_run_key() -> bool:
        """Delete the HKCU Run-key value. Returns True if a value was removed."""
        # Check existence first to avoid noisy errors.
        query = _run(
            [
                "reg",
                "query",
                r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                "/v",
                ITUNES_RUN_VALUE_NAME,
            ]
        )
        if query.returncode != 0:
            return False

        result = _run(
            [
                "reg",
                "delete",
                r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                "/v",
                ITUNES_RUN_VALUE_NAME,
                "/f",
            ]
        )
        if result.returncode != 0:
            logger.error(
                "reg delete failed: %s",
                (result.stderr or result.stdout or "").strip(),
            )
            return False
        return True

    @staticmethod
    def _query_app_paths(exe_name: str) -> str | None:
        """Look up an executable in the Windows ``App Paths`` registry key."""
        for hive in ("HKCU", "HKLM"):
            key = (
                rf"{hive}\Software\Microsoft\Windows\CurrentVersion"
                rf"\App Paths\{exe_name}"
            )
            result = _run(["reg", "query", key, "/ve"])
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                line = line.strip()
                if "REG_SZ" in line or "REG_EXPAND_SZ" in line:
                    parts = line.split(None, 2)
                    if len(parts) >= 3:
                        return os.path.expandvars(parts[2].strip().strip('"'))
        return None


__all__ = ["SchedulerSetup", "TASK_NAME"]
