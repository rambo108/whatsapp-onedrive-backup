"""Sync WhatsApp backup files to/from the local OneDrive sync folder.

Copies files into the OneDrive desktop app's sync folder and lets the app
handle cloud upload. No OAuth or Graph API required.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_ROOT = "WhatsApp-Backups"
TIMESTAMP_FMT = "%Y-%m-%d_%H%M%S"
METADATA_FILE = "backup_meta.json"


@dataclass
class OneDriveSync:
    """Manage WhatsApp backups inside the local OneDrive sync folder."""

    # Optional override – when set, skip auto-detection.
    onedrive_folder_override: Path | None = None
    _cached_folder: Path | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # OneDrive folder detection
    # ------------------------------------------------------------------

    def find_onedrive_folder(self) -> Path | None:
        """Auto-detect the OneDrive sync folder on Windows.

        Checks, in order:
        1. Explicit override supplied at construction time.
        2. Environment variables (``OneDrive``, ``OneDriveConsumer``,
           ``OneDriveCommercial``).
        3. The Windows registry
           (``HKCU\\Software\\Microsoft\\OneDrive – UserFolder``).
        4. Common default paths under ``%USERPROFILE%``.

        Returns the first existing directory found, or *None*.
        """
        if self.onedrive_folder_override and self.onedrive_folder_override.is_dir():
            logger.debug("Using override folder: %s", self.onedrive_folder_override)
            return self.onedrive_folder_override

        if self._cached_folder and self._cached_folder.is_dir():
            return self._cached_folder

        # --- environment variables ---
        env_vars = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
        for var in env_vars:
            value = os.environ.get(var)
            if value:
                candidate = Path(value)
                if candidate.is_dir():
                    logger.info("OneDrive folder found via %%%s%%: %s", var, candidate)
                    self._cached_folder = candidate
                    return candidate

        # --- Windows registry ---
        folder = self._read_registry_folder()
        if folder:
            self._cached_folder = folder
            return folder

        # --- common default paths ---
        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile:
            defaults = (
                Path(user_profile, "OneDrive"),
                Path(user_profile, "OneDrive - Personal"),
            )
            for candidate in defaults:
                if candidate.is_dir():
                    logger.info("OneDrive folder found at default path: %s", candidate)
                    self._cached_folder = candidate
                    return candidate

        logger.warning("Could not auto-detect OneDrive sync folder")
        return None

    @staticmethod
    def _read_registry_folder() -> Path | None:
        """Read the OneDrive user folder from the Windows registry."""
        try:
            import winreg  # noqa: WPS433 – Windows-only import
        except ImportError:
            logger.debug("winreg not available (non-Windows platform)")
            return None

        reg_paths = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive", "UserFolder"),
            (
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\OneDrive\Accounts\Personal",
                "UserFolder",
            ),
            (
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\OneDrive\Accounts\Business1",
                "UserFolder",
            ),
        )

        for hive, subkey, value_name in reg_paths:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    folder_str, _ = winreg.QueryValueEx(key, value_name)
                    candidate = Path(folder_str)
                    if candidate.is_dir():
                        logger.info(
                            "OneDrive folder found via registry (%s): %s",
                            subkey,
                            candidate,
                        )
                        return candidate
            except OSError:
                continue

        return None

    # ------------------------------------------------------------------
    # Sync (upload)
    # ------------------------------------------------------------------

    def sync_to_onedrive(
        self,
        source_dir: Path,
        onedrive_folder: Path,
        backup_name: str | None = None,
    ) -> Path:
        """Copy *source_dir* into a versioned backup folder inside OneDrive.

        Parameters
        ----------
        source_dir:
            Local directory containing the extracted WhatsApp backup files.
        onedrive_folder:
            Root of the OneDrive sync folder (e.g. ``~/OneDrive``).
        backup_name:
            Optional human-readable name.  Defaults to a UTC timestamp.

        Returns
        -------
        Path
            The destination directory inside OneDrive.

        Raises
        ------
        FileNotFoundError
            If *source_dir* does not exist.
        OSError
            If the copy operation fails.
        """
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

        timestamp = datetime.now(tz=timezone.utc).strftime(TIMESTAMP_FMT)
        folder_name = backup_name or timestamp

        dest = onedrive_folder / BACKUP_ROOT / folder_name
        dest.mkdir(parents=True, exist_ok=True)

        logger.info("Syncing backup to OneDrive: %s -> %s", source_dir, dest)

        for item in source_dir.iterdir():
            dst_path = dest / item.name
            if item.is_dir():
                shutil.copytree(item, dst_path, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst_path)

        # Write a small metadata sidecar so we can retrieve info later.
        self._write_metadata(dest, source_dir, timestamp)

        logger.info("Backup synced successfully: %s", dest)
        return dest

    @staticmethod
    def _write_metadata(dest: Path, source_dir: Path, timestamp: str) -> None:
        meta = {
            "source": str(source_dir),
            "timestamp_utc": timestamp,
            "synced_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        meta_path = dest / METADATA_FILE
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # List backups
    # ------------------------------------------------------------------

    def list_backups(self, onedrive_folder: Path) -> list[dict]:
        """Return available backup versions sorted newest-first.

        Each dict contains:
        - ``name``  – folder name (typically the timestamp)
        - ``path``  – full ``Path`` to the backup folder
        - ``date``  – parsed ``datetime`` if the name is a valid timestamp,
          else *None*
        - ``size_bytes`` – total size of all files in the backup
        - ``file_count`` – number of files
        """
        backup_root = onedrive_folder / BACKUP_ROOT
        if not backup_root.is_dir():
            logger.debug("Backup root does not exist: %s", backup_root)
            return []

        backups: list[dict] = []
        for entry in backup_root.iterdir():
            if not entry.is_dir():
                continue

            date = self._parse_backup_date(entry.name)
            size, count = self._dir_stats(entry)

            backups.append(
                {
                    "name": entry.name,
                    "path": entry,
                    "date": date,
                    "size_bytes": size,
                    "file_count": count,
                }
            )

        backups.sort(key=lambda b: b["date"] or datetime.min, reverse=True)
        return backups

    @staticmethod
    def _parse_backup_date(name: str) -> datetime | None:
        try:
            return datetime.strptime(name, TIMESTAMP_FMT)
        except ValueError:
            return None

    @staticmethod
    def _dir_stats(directory: Path) -> tuple[int, int]:
        """Return ``(total_bytes, file_count)`` for *directory*."""
        total = 0
        count = 0
        for path in directory.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
                count += 1
        return total, count

    # ------------------------------------------------------------------
    # Restore (download)
    # ------------------------------------------------------------------

    def restore_from_onedrive(
        self,
        onedrive_folder: Path,
        backup_name: str,
        dest_dir: Path,
    ) -> Path:
        """Copy a backup version from the OneDrive folder to *dest_dir*.

        Parameters
        ----------
        onedrive_folder:
            Root of the OneDrive sync folder.
        backup_name:
            Name of the backup subfolder to restore (e.g. a timestamp).
        dest_dir:
            Local directory to copy the backup into.  A subfolder named
            after the backup is created inside *dest_dir*.

        Returns
        -------
        Path
            The restored directory.

        Raises
        ------
        FileNotFoundError
            If the requested backup does not exist.
        """
        src = onedrive_folder / BACKUP_ROOT / backup_name
        if not src.is_dir():
            raise FileNotFoundError(f"Backup not found: {src}")

        restored = dest_dir / backup_name
        restored.mkdir(parents=True, exist_ok=True)

        logger.info("Restoring backup: %s -> %s", src, restored)

        for item in src.iterdir():
            dst_path = restored / item.name
            if item.is_dir():
                shutil.copytree(item, dst_path, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst_path)

        logger.info("Restore complete: %s", restored)
        return restored

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def apply_retention(
        self, onedrive_folder: Path, keep_versions: int = 5
    ) -> list[Path]:
        """Delete old backups that exceed the retention limit.

        Backups are sorted newest-first; the oldest beyond
        *keep_versions* are removed.

        Returns the list of deleted directories.
        """
        if keep_versions < 1:
            raise ValueError("keep_versions must be >= 1")

        backups = self.list_backups(onedrive_folder)
        to_delete = backups[keep_versions:]

        deleted: list[Path] = []
        for backup in to_delete:
            path: Path = backup["path"]
            logger.info("Retention: removing old backup %s", path)
            try:
                shutil.rmtree(path)
                deleted.append(path)
            except OSError:
                logger.exception("Failed to delete backup: %s", path)

        if deleted:
            logger.info("Retention: deleted %d old backup(s)", len(deleted))
        return deleted

    # ------------------------------------------------------------------
    # OneDrive process check
    # ------------------------------------------------------------------

    @staticmethod
    def is_onedrive_running() -> bool:
        """Return *True* if the OneDrive desktop app process is active."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq OneDrive.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "OneDrive.exe" in result.stdout
        except (subprocess.SubprocessError, OSError):
            logger.debug("Failed to query process list", exc_info=True)
            return False
