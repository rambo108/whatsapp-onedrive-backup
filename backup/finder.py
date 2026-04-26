"""Find and identify iTunes iPhone backups on Windows."""

from __future__ import annotations

import logging
import os
import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Standard iTunes backup location and Apple Devices app location
_BACKUP_ROOTS: list[Path] = [
    Path(os.environ.get("APPDATA", "")) / "Apple Computer" / "MobileSync" / "Backup",
    Path(os.environ.get("USERPROFILE", "")) / "Apple" / "MobileSync" / "Backup",
]


@dataclass(frozen=True)
class BackupInfo:
    """Metadata for a single iTunes iPhone backup."""

    path: Path
    device_name: str = ""
    ios_version: str = ""
    build_version: str = ""
    last_backup_date: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc))
    udid: str = ""
    encrypted: bool = False


class BackupFinder:
    """Locate and inspect iTunes iPhone backups on disk."""

    def __init__(self, extra_roots: list[Path] | None = None) -> None:
        self._roots = list(_BACKUP_ROOTS)
        if extra_roots:
            self._roots.extend(extra_roots)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_backup_dirs(self) -> list[Path]:
        """Return every backup folder found under the known root directories."""
        dirs: list[Path] = []
        for root in self._roots:
            if not root.is_dir():
                logger.debug("Backup root does not exist: %s", root)
                continue
            for entry in root.iterdir():
                if entry.is_dir() and (entry / "Info.plist").exists():
                    dirs.append(entry)
        logger.info("Found %d backup folder(s)", len(dirs))
        return dirs

    def get_backup_info(self, backup_dir: Path) -> BackupInfo:
        """Parse Info.plist and Manifest.plist to build a BackupInfo."""
        info_plist = backup_dir / "Info.plist"
        manifest_plist = backup_dir / "Manifest.plist"

        info = _read_plist(info_plist)
        manifest = _read_plist(manifest_plist)

        last_backup_date = info.get("Last Backup Date", datetime.min)
        if isinstance(last_backup_date, datetime) and last_backup_date.tzinfo is None:
            last_backup_date = last_backup_date.replace(tzinfo=timezone.utc)

        return BackupInfo(
            path=backup_dir,
            device_name=str(info.get("Device Name", "")),
            ios_version=str(info.get("Product Version", "")),
            build_version=str(info.get("Build Version", "")),
            last_backup_date=last_backup_date,
            udid=str(info.get("Target Identifier", info.get("UDID", ""))),
            encrypted=bool(manifest.get("IsEncrypted", False)),
        )

    def list_backups(self) -> list[BackupInfo]:
        """Return all discovered backups sorted by date, newest first."""
        backups: list[BackupInfo] = []
        for d in self.find_backup_dirs():
            try:
                backups.append(self.get_backup_info(d))
            except Exception:
                logger.warning("Skipping unreadable backup: %s", d, exc_info=True)
        backups.sort(key=lambda b: b.last_backup_date, reverse=True)
        return backups

    def get_latest_backup(self) -> BackupInfo | None:
        """Return the most recent backup, or None if no backups exist."""
        backups = self.list_backups()
        return backups[0] if backups else None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _read_plist(path: Path) -> dict:
    """Read a binary or XML plist, returning an empty dict on failure."""
    if not path.exists():
        logger.debug("Plist not found: %s", path)
        return {}
    try:
        with path.open("rb") as fh:
            return plistlib.load(fh)
    except Exception:
        logger.warning("Failed to parse plist: %s", path, exc_info=True)
        return {}
