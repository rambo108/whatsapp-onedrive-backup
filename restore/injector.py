"""
Selective WhatsApp restore via iTunes backup injection.

WARNING: This module is EXPERIMENTAL. It modifies iTunes backup databases and
files in ways that Apple does not officially support. Incorrect modifications
can render a backup unrestorable. Always keep an unmodified copy of your backup.

Strategy:
    1. User creates a fresh iTunes backup of their current phone state.
    2. This module copies that backup (never touches the original).
    3. In the copy, it replaces WhatsApp entries in Manifest.db and swaps the
       corresponding data files on disk.
    4. The user restores the modified copy via iTunes — the phone keeps all
       current data except WhatsApp, which comes from the OneDrive archive.

Disk layout inside an iTunes backup:
    backup_dir/
        Manifest.db          — SQLite database listing every file
        Info.plist
        Status.plist
        <aa>/                — two-char hex prefix directories
            <fileID>         — actual file blobs, named by SHA-1 fileID
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

WHATSAPP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
WHATSAPP_DOMAINS = frozenset(
    {
        WHATSAPP_DOMAIN,
        "AppDomain-net.whatsapp.WhatsApp",
        "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
    }
)

# Manifest.db schema (Files table):
#   fileID  TEXT PRIMARY KEY,
#   domain  TEXT,
#   relativePath TEXT,
#   flags   INTEGER,
#   file    BLOB          -- binary plist with size / hash / metadata


@dataclass
class InjectionResult:
    """Summary of a completed injection operation."""

    files_updated: int = 0
    files_added: int = 0
    files_removed: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class RestoreInjector:
    """Injects WhatsApp files from a OneDrive archive into an iTunes backup."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_backup(self, backup_dir: Path) -> bool:
        """Return True if *backup_dir* looks like a valid iTunes backup.

        Checks for the presence of ``Manifest.db`` and a few other expected
        files.  Does **not** verify cryptographic integrity.
        """
        backup_dir = Path(backup_dir)

        if not backup_dir.is_dir():
            logger.error("Backup directory does not exist: %s", backup_dir)
            return False

        manifest = backup_dir / "Manifest.db"
        if not manifest.is_file():
            logger.error("Manifest.db not found in %s", backup_dir)
            return False

        # Quick sanity check — can we open the database?
        try:
            with sqlite3.connect(str(manifest)) as conn:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='Files'"
                )
                if cursor.fetchone() is None:
                    logger.error("Manifest.db does not contain a 'Files' table")
                    return False
        except sqlite3.Error as exc:
            logger.error("Failed to read Manifest.db: %s", exc)
            return False

        # Optional but expected companion files
        for name in ("Info.plist", "Status.plist"):
            if not (backup_dir / name).exists():
                logger.warning("Expected file %s missing — backup may be incomplete", name)

        logger.info("Backup at %s looks valid", backup_dir)
        return True

    def create_backup_copy(self, backup_dir: Path) -> Path:
        """Create a full copy of the backup directory for safe modification.

        Returns the ``Path`` to the new copy.  The copy is placed next to the
        original with a ``_whatsapp_restore`` suffix and a timestamp so it
        never collides with an existing directory.
        """
        backup_dir = Path(backup_dir)
        if not backup_dir.is_dir():
            raise FileNotFoundError(f"Backup directory not found: {backup_dir}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        copy_name = f"{backup_dir.name}_whatsapp_restore_{timestamp}"
        copy_dir = backup_dir.parent / copy_name

        logger.info("Copying backup to %s — this may take a while …", copy_dir)
        shutil.copytree(str(backup_dir), str(copy_dir))
        logger.info("Backup copy created successfully")
        return copy_dir

    def inject_whatsapp(
        self, backup_dir: Path, whatsapp_source_dir: Path
    ) -> InjectionResult:
        """Replace WhatsApp data inside an iTunes backup copy.

        Parameters
        ----------
        backup_dir:
            Path to a **copy** of an iTunes backup (see
            :meth:`create_backup_copy`).  This directory **will be modified**.
        whatsapp_source_dir:
            Directory containing the WhatsApp files previously extracted from
            OneDrive.  The files should be organised in the same relative-path
            layout as the original WhatsApp container
            (``Documents/``, ``Library/``, etc.).

        Returns
        -------
        InjectionResult
            Counts of updated / added / removed files.
        """
        backup_dir = Path(backup_dir)
        whatsapp_source_dir = Path(whatsapp_source_dir)

        logger.warning(
            "⚠️  EXPERIMENTAL: Modifying iTunes backup at %s. "
            "Make sure this is a COPY, not your only backup!",
            backup_dir,
        )

        if not self.validate_backup(backup_dir):
            raise ValueError(f"Invalid or corrupt backup at {backup_dir}")
        if not whatsapp_source_dir.is_dir():
            raise FileNotFoundError(
                f"WhatsApp source directory not found: {whatsapp_source_dir}"
            )

        result = InjectionResult()
        manifest_path = backup_dir / "Manifest.db"

        # Collect source files (relative paths under whatsapp_source_dir)
        source_files: Dict[str, Path] = {}
        for file_path in whatsapp_source_dir.rglob("*"):
            if file_path.is_file():
                rel = file_path.relative_to(whatsapp_source_dir).as_posix()
                source_files[rel] = file_path

        if not source_files:
            logger.warning("No files found in %s — nothing to inject", whatsapp_source_dir)
            return result

        logger.info("Found %d WhatsApp files to inject", len(source_files))

        with sqlite3.connect(str(manifest_path)) as conn:
            conn.row_factory = sqlite3.Row

            # --- Phase 1: Build index of existing WhatsApp rows --------
            existing: Dict[str, sqlite3.Row] = {}
            for row in conn.execute(
                "SELECT fileID, domain, relativePath, flags, file "
                "FROM Files WHERE domain IN ({})".format(
                    ",".join("?" for _ in WHATSAPP_DOMAINS)
                ),
                tuple(WHATSAPP_DOMAINS),
            ):
                existing[row["relativePath"]] = row

            matched_paths: Set[str] = set()

            # --- Phase 2: Upsert source files --------------------------
            for rel_path, src_path in source_files.items():
                file_id = self._compute_file_id(WHATSAPP_DOMAIN, rel_path)

                if rel_path in existing:
                    # Update existing entry
                    old_id = existing[rel_path]["fileID"]
                    self._remove_blob(backup_dir, old_id)
                    self._copy_blob(src_path, backup_dir, file_id)
                    file_blob = self._build_file_blob(src_path)
                    conn.execute(
                        "UPDATE Files SET fileID = ?, file = ? "
                        "WHERE domain = ? AND relativePath = ?",
                        (file_id, file_blob, existing[rel_path]["domain"], rel_path),
                    )
                    result.files_updated += 1
                    matched_paths.add(rel_path)
                    logger.debug("Updated: %s", rel_path)
                else:
                    # Insert new entry
                    self._copy_blob(src_path, backup_dir, file_id)
                    file_blob = self._build_file_blob(src_path)
                    conn.execute(
                        "INSERT INTO Files (fileID, domain, relativePath, flags, file) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (file_id, WHATSAPP_DOMAIN, rel_path, 1, file_blob),
                    )
                    result.files_added += 1
                    matched_paths.add(rel_path)
                    logger.debug("Added: %s", rel_path)

            # --- Phase 3: Remove stale WhatsApp entries ----------------
            for rel_path, row in existing.items():
                if rel_path not in matched_paths:
                    self._remove_blob(backup_dir, row["fileID"])
                    conn.execute(
                        "DELETE FROM Files WHERE fileID = ?",
                        (row["fileID"],),
                    )
                    result.files_removed += 1
                    logger.debug("Removed stale: %s", rel_path)

            conn.commit()

        logger.info(
            "Injection complete — updated: %d, added: %d, removed: %d",
            result.files_updated,
            result.files_added,
            result.files_removed,
        )
        return result

    def verify_injection(self, backup_dir: Path) -> bool:
        """Verify every WhatsApp entry in Manifest.db has a matching blob on disk.

        Returns ``True`` if all references are satisfied, ``False`` otherwise.
        """
        backup_dir = Path(backup_dir)
        manifest_path = backup_dir / "Manifest.db"

        if not manifest_path.is_file():
            logger.error("Manifest.db not found in %s", backup_dir)
            return False

        ok = True
        count = 0

        with sqlite3.connect(str(manifest_path)) as conn:
            rows = conn.execute(
                "SELECT fileID, relativePath FROM Files WHERE domain IN ({})".format(
                    ",".join("?" for _ in WHATSAPP_DOMAINS)
                ),
                tuple(WHATSAPP_DOMAINS),
            ).fetchall()

            for file_id, rel_path in rows:
                blob_path = self._blob_path(backup_dir, file_id)
                if not blob_path.is_file():
                    logger.error(
                        "Missing blob for %s (fileID %s, expected %s)",
                        rel_path,
                        file_id,
                        blob_path,
                    )
                    ok = False
                count += 1

        if ok:
            logger.info("Verification passed — %d WhatsApp entries all have blobs", count)
        else:
            logger.error("Verification FAILED — some blobs are missing")

        return ok

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_file_id(domain: str, relative_path: str) -> str:
        """Derive the fileID (SHA-1 hex digest) used by iTunes.

        iTunes computes ``SHA1(domain + "-" + relativePath)`` to get the 40-char
        hex filename used on disk.
        """
        raw = f"{domain}-{relative_path}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _blob_path(backup_dir: Path, file_id: str) -> Path:
        """Return the on-disk path for a given fileID inside the backup."""
        return backup_dir / file_id[:2] / file_id

    def _copy_blob(self, src: Path, backup_dir: Path, file_id: str) -> None:
        """Copy *src* into the backup tree at the correct fileID location."""
        dest = self._blob_path(backup_dir, file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))

    def _remove_blob(self, backup_dir: Path, file_id: str) -> None:
        """Remove a blob from the backup tree if it exists."""
        path = self._blob_path(backup_dir, file_id)
        if path.is_file():
            path.unlink()

    @staticmethod
    def _build_file_blob(src: Path) -> bytes:
        """Build a minimal metadata blob for the Manifest.db ``file`` column.

        Real iTunes backups store a binary plist here. For our purposes we
        store a JSON-encoded dict with the fields iTunes checks during restore:
        size, last-modified time, and a SHA-256 hash of the contents.

        .. note::

           A full implementation would encode this as a binary plist using
           ``plistlib``.  The JSON stand-in is sufficient for many
           third-party restore tools but may not satisfy official iTunes
           validation on all versions.
        """
        stat = src.stat()
        sha256 = hashlib.sha256(src.read_bytes()).hexdigest()
        meta = {
            "Size": stat.st_size,
            "LastModified": int(stat.st_mtime),
            "SHA256": sha256,
        }
        return json.dumps(meta).encode("utf-8")
