"""Extract WhatsApp-related files from an iTunes iPhone backup.

Opens the backup's Manifest.db, queries for WhatsApp domains, and copies
matching files to a destination directory while preserving relative paths.
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

logger = logging.getLogger(__name__)

WHATSAPP_DOMAINS = (
    "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
    "AppDomain-net.whatsapp.WhatsApp",
)


@dataclass
class ExtractionResult:
    """Metadata produced by a single extraction run."""

    total_files: int = 0
    total_size: int = 0
    extracted_at: str = ""
    files: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


class WhatsAppExtractor:
    """Copies WhatsApp files out of an iTunes backup into a flat directory."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_whatsapp_files(self, backup_dir: Path) -> list[dict]:
        """Return every WhatsApp row from the backup's Manifest.db.

        Each dict contains *domain*, *relative_path*, *file_id*, and *flags*.
        """
        manifest_db = self._manifest_path(backup_dir)
        self._check_encryption(backup_dir)

        rows: list[dict] = []
        placeholders = ",".join("?" for _ in WHATSAPP_DOMAINS)
        query = (
            f"SELECT fileID, domain, relativePath, flags "
            f"FROM Files WHERE domain IN ({placeholders})"
        )

        con = sqlite3.connect(str(manifest_db))
        try:
            con.row_factory = sqlite3.Row
            for row in con.execute(query, WHATSAPP_DOMAINS):
                rows.append(
                    {
                        "file_id": row["fileID"],
                        "domain": row["domain"],
                        "relative_path": row["relativePath"],
                        "flags": row["flags"],
                    }
                )
        finally:
            con.close()

        logger.info("Found %d WhatsApp files in manifest", len(rows))
        return rows

    def extract(self, backup_dir: Path, dest_dir: Path) -> ExtractionResult:
        """Extract all WhatsApp files from *backup_dir* into *dest_dir*.

        Files are laid out as ``dest_dir / domain / relativePath``.
        """
        whatsapp_files = self.list_whatsapp_files(backup_dir)

        result = ExtractionResult(
            extracted_at=datetime.now(timezone.utc).isoformat(),
        )

        for entry in whatsapp_files:
            file_id: str = entry["file_id"]
            domain: str = entry["domain"]
            relative_path: str = entry["relative_path"]
            flags: int = entry["flags"]

            # flags == 2 means directory – nothing to copy
            if flags == 2:
                logger.debug("Skipping directory entry: %s/%s", domain, relative_path)
                continue

            source = backup_dir / file_id[:2] / file_id
            if not source.exists():
                logger.warning(
                    "Source blob missing for %s (%s/%s) – skipping",
                    file_id,
                    domain,
                    relative_path,
                )
                result.skipped.append(
                    {
                        "file_id": file_id,
                        "domain": domain,
                        "relative_path": relative_path,
                        "reason": "source_missing",
                    }
                )
                continue

            dest_path = dest_dir / domain / relative_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            shutil.copy2(str(source), str(dest_path))
            file_size = dest_path.stat().st_size

            result.total_files += 1
            result.total_size += file_size
            result.files.append(
                {
                    "file_id": file_id,
                    "domain": domain,
                    "relative_path": relative_path,
                    "size": file_size,
                }
            )
            logger.debug("Extracted %s/%s (%d bytes)", domain, relative_path, file_size)

        logger.info(
            "Extraction complete: %d files, %d bytes total",
            result.total_files,
            result.total_size,
        )
        return result

    def compute_manifest(self, dest_dir: Path) -> dict[str, str]:
        """Return ``{relative_posix_path: sha256_hex}`` for every file under *dest_dir*."""
        manifest: dict[str, str] = {}
        for root, _dirs, files in os.walk(dest_dir):
            for name in files:
                full = Path(root) / name
                rel = full.relative_to(dest_dir).as_posix()
                manifest[rel] = self._sha256(full)
        logger.info("Computed manifest with %d entries", len(manifest))
        return manifest

    def has_changes(
        self,
        current_manifest: dict[str, str],
        previous_manifest_path: Path,
    ) -> bool:
        """Compare *current_manifest* against a previously saved manifest file.

        Returns ``True`` when any file was added, removed, or modified.
        """
        if not previous_manifest_path.exists():
            logger.info("No previous manifest at %s – treating as changed", previous_manifest_path)
            return True

        with open(previous_manifest_path, "r", encoding="utf-8") as fh:
            previous: dict[str, str] = json.load(fh)

        if current_manifest == previous:
            logger.info("Manifests match – no changes detected")
            return False

        added = set(current_manifest) - set(previous)
        removed = set(previous) - set(current_manifest)
        modified = {
            k
            for k in set(current_manifest) & set(previous)
            if current_manifest[k] != previous[k]
        }

        logger.info(
            "Changes detected: %d added, %d removed, %d modified",
            len(added),
            len(removed),
            len(modified),
        )
        return True

    def save_manifest(self, manifest: dict[str, str], path: Path) -> None:
        """Persist *manifest* as pretty-printed JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
        logger.info("Manifest saved to %s", path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _manifest_path(backup_dir: Path) -> Path:
        db = backup_dir / "Manifest.db"
        if not db.exists():
            raise FileNotFoundError(
                f"Manifest.db not found in {backup_dir}. "
                "Verify this is a valid iTunes backup directory."
            )
        return db

    @staticmethod
    def _check_encryption(backup_dir: Path) -> None:
        """Raise if the backup appears to be encrypted."""
        manifest_plist = backup_dir / "Manifest.plist"
        if not manifest_plist.exists():
            return

        # Manifest.plist is a binary plist; the encryption flag is stored as
        # the key "IsEncrypted".  A quick byte-level check avoids pulling in
        # a full plist parser (keeping stdlib-only).
        try:
            data = manifest_plist.read_bytes()
            if b"IsEncrypted" in data:
                # Look for the boolean-true marker immediately after the key.
                idx = data.find(b"IsEncrypted")
                # In binary plists the boolean true byte is 0x09.
                # In XML plists the tag <true/> follows the key.
                region = data[idx : idx + 80]
                if b"<true/>" in region or b"\x09" in region:
                    raise EncryptedBackupError(
                        "This backup is encrypted. Decrypt it first with "
                        "backup/decrypt.py before extracting WhatsApp files."
                    )
        except EncryptedBackupError:
            raise
        except Exception:
            logger.debug("Could not parse Manifest.plist for encryption check", exc_info=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()


class EncryptedBackupError(Exception):
    """Raised when the backup is encrypted and must be decrypted first."""
