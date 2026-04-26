"""
Decryption module for encrypted iTunes iPhone backups.

Encryption scheme overview
--------------------------
iTunes encrypts iPhone backups using a multi-layer key hierarchy:

1. **Backup password** — the user-chosen password set in iTunes/Finder.
2. **Key derivation** — the password is fed through PBKDF2 to derive a
   passphrase key.  iOS 10.2+ uses double PBKDF2: first PBKDF2-HMAC-SHA256
   with 10,000,000 iterations, then HMAC-SHA256 with a fixed "salt" string
   to produce the actual decryption key.
3. **Keybag** — stored in ``Manifest.plist`` as a binary blob.  It holds a
   set of *class keys*, each protecting a different protection class
   (NSFileProtection levels).  The class keys are AES-wrapped with the
   derived passphrase key.
4. **ManifestKey** — also in ``Manifest.plist``.  It is the per-file key
   used to encrypt ``Manifest.db`` itself (protection class 3 / 4).
5. **Per-file encryption** — every backed-up file is encrypted with
   AES-256-CBC using a per-file key stored in the manifest.  The per-file
   key is itself wrapped with the class key for the file's protection class.

References
----------
* https://support.apple.com/guide/security/backup-keybag-sec21f866f54/web
* https://www.theiphonewiki.com/wiki/ITunes_Backup
* ``iphone_backup_extractor`` (open-source tool for the same job)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import plistlib
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency: cryptography (preferred) for AES
# ---------------------------------------------------------------------------
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.backends import default_backend

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False

# ---------------------------------------------------------------------------
# Optional dependency: biplist for binary-plist parsing
# ---------------------------------------------------------------------------
try:
    import biplist  # type: ignore[import-untyped]

    _HAS_BIPLIST = True
except ImportError:
    _HAS_BIPLIST = False

# ---------------------------------------------------------------------------
# Constants used by the keybag / key-derivation scheme
# ---------------------------------------------------------------------------

# Tag identifiers found inside the keybag binary blob.
KEYBAG_TAGS = {
    b"VERS": 0,  # keybag version
    b"TYPE": 1,  # keybag type (backup = 1)
    b"UUID": 2,  # keybag UUID
    b"HMCK": 3,  # HMAC key
    b"WRAP": 4,  # wrap type (password-based = 2)
    b"SALT": 5,  # PBKDF2 salt
    b"ITER": 6,  # PBKDF2 iteration count
    b"CLAS": 7,  # protection class
    b"KTYP": 8,  # key type
    b"WPKY": 9,  # wrapped (encrypted) key
    b"DPWT": 10, # double-protection wrap type
    b"DPIC": 11, # double-protection iteration count
    b"DPSL": 12, # double-protection salt
}

# The fixed string used in the second HMAC-SHA256 step for iOS 10.2+.
IOS_102_HMAC_FIXED_STRING = b"OFB\x05GbackupKeybag"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ClassKey:
    """A single protection-class key entry from the keybag."""

    protection_class: int = 0
    wrap_type: int = 0
    key_type: int = 0
    wrapped_key: bytes = b""
    unwrapped_key: bytes = b""  # populated after unlock


@dataclass
class Keybag:
    """
    Parsed representation of the keybag stored in ``Manifest.plist``.

    The keybag is a flat TLV (tag-length-value) structure:
    ``[4-byte tag][4-byte big-endian length][value bytes] …``
    """

    version: int = 0
    bag_type: int = 0
    uuid: bytes = b""
    hmac_key: bytes = b""
    wrap_type: int = 0
    salt: bytes = b""
    iterations: int = 0
    # iOS 10.2+ double-protection fields
    dp_wrap_type: int = 0
    dp_iterations: int = 0
    dp_salt: bytes = b""
    class_keys: Dict[int, ClassKey] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes) -> "Keybag":
        """
        Parse the raw keybag blob into a :class:`Keybag`.

        The blob is a sequence of TLV entries:
        ``[4-char tag][4-byte BE length][<length> bytes of value] …``

        Top-level tags set keybag-wide attributes; once a ``CLAS`` tag is
        encountered, all subsequent tags apply to the *current* class key
        until the next ``CLAS``.
        """
        kb = cls()
        current_key: Optional[ClassKey] = None
        offset = 0

        while offset + 8 <= len(data):
            tag = data[offset : offset + 4]
            length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
            value = data[offset + 8 : offset + 8 + length]
            offset += 8 + length

            # Helper to read a 4-byte big-endian int from value
            int_val = (
                struct.unpack(">I", value)[0] if len(value) == 4 else None
            )

            # ``CLAS`` starts a new class-key record
            if tag == b"CLAS":
                current_key = ClassKey(protection_class=int_val or 0)
                kb.class_keys[current_key.protection_class] = current_key
                continue

            # Tags that belong to a class key (if we're inside one)
            if current_key is not None:
                if tag == b"WRAP":
                    current_key.wrap_type = int_val or 0
                elif tag == b"KTYP":
                    current_key.key_type = int_val or 0
                elif tag == b"WPKY":
                    current_key.wrapped_key = value
                continue

            # Top-level tags
            if tag == b"VERS":
                kb.version = int_val or 0
            elif tag == b"TYPE":
                kb.bag_type = int_val or 0
            elif tag == b"UUID":
                kb.uuid = value
            elif tag == b"HMCK":
                kb.hmac_key = value
            elif tag == b"WRAP":
                kb.wrap_type = int_val or 0
            elif tag == b"SALT":
                kb.salt = value
            elif tag == b"ITER":
                kb.iterations = int_val or 0
            elif tag == b"DPWT":
                kb.dp_wrap_type = int_val or 0
            elif tag == b"DPIC":
                kb.dp_iterations = int_val or 0
            elif tag == b"DPSL":
                kb.dp_salt = value

        logger.debug(
            "Parsed keybag: version=%d, type=%d, %d class keys, "
            "PBKDF2 iterations=%d, double-protection iterations=%d",
            kb.version,
            kb.bag_type,
            len(kb.class_keys),
            kb.iterations,
            kb.dp_iterations,
        )
        return kb


# ---------------------------------------------------------------------------
# AES helpers
# ---------------------------------------------------------------------------

def _aes_decrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """
    Decrypt *data* with AES-256-CBC using *key* and *iv*.

    Requires the ``cryptography`` package.  Raises a clear error if it is
    not installed.
    """
    if not _HAS_CRYPTOGRAPHY:
        raise ImportError(
            "The 'cryptography' package is required for AES decryption.\n"
            "Install it with:  pip install cryptography\n"
            "Alternatively, use a tool like 'iphone_backup_extractor' to "
            "decrypt the backup externally."
        )
    cipher = Cipher(
        algorithms.AES(key), modes.CBC(iv), backend=default_backend()
    )
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _aes_unwrap_key(kek: bytes, wrapped: bytes) -> bytes:
    """
    AES key-unwrap (RFC 3394).

    The keybag's class keys are wrapped (encrypted) with the passphrase-
    derived key using the AES-wrap algorithm.

    Parameters
    ----------
    kek : bytes
        Key-encryption key (the derived passphrase key), 16 or 32 bytes.
    wrapped : bytes
        The wrapped key (class key), ``len(key) + 8`` bytes.

    Returns
    -------
    bytes
        The unwrapped (plaintext) key.

    Raises
    ------
    ValueError
        If the integrity check fails (wrong password / corrupted data).
    """
    if not _HAS_CRYPTOGRAPHY:
        raise ImportError(
            "The 'cryptography' package is required for AES key unwrap.\n"
            "Install it with:  pip install cryptography"
        )
    # Use cryptography's key-unwrap if available (cleaner than manual RFC 3394)
    try:
        from cryptography.hazmat.primitives.keywrap import aes_key_unwrap

        return aes_key_unwrap(kek, wrapped, default_backend())
    except Exception as exc:
        raise ValueError(
            "Failed to unwrap class key — the backup password is likely "
            "incorrect."
        ) from exc


def _strip_pkcs7_padding(data: bytes) -> bytes:
    """Remove PKCS#7 padding from *data*."""
    if not data:
        return data
    pad_len = data[-1]
    # Sanity: padding byte must be 1..block_size and all padding bytes equal
    if pad_len < 1 or pad_len > 16:
        return data
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data
    return data[:-pad_len]


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class BackupDecryptor:
    """
    Decrypt an encrypted iTunes iPhone backup.

    Usage::

        decryptor = BackupDecryptor(Path("path/to/backup"), "MyPassword")
        if decryptor.unlock():
            decrypted_db = decryptor.decrypt_manifest_db()
            # … use decrypted_db as a normal Manifest.db …

    Parameters
    ----------
    backup_dir : Path
        Directory containing the iTunes backup (must have ``Manifest.plist``).
    password : str
        The backup encryption password set in iTunes / Finder.
    """

    def __init__(self, backup_dir: Path, password: str) -> None:
        self.backup_dir = Path(backup_dir)
        self.password = password
        self.keybag: Optional[Keybag] = None
        self.manifest_key: bytes = b""
        self.manifest_class: int = 0
        self._unlocked = False

        # Eagerly parse Manifest.plist so we fail fast on bad paths.
        self._manifest_plist = self._load_manifest_plist()
        self._parse_keybag_and_manifest_key()

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_encrypted(backup_dir: Path) -> bool:
        """
        Return ``True`` if the backup at *backup_dir* is encrypted.

        Checks the ``IsEncrypted`` key in ``Manifest.plist``.
        """
        manifest_plist_path = Path(backup_dir) / "Manifest.plist"
        if not manifest_plist_path.exists():
            logger.warning("Manifest.plist not found in %s", backup_dir)
            return False

        plist = _read_plist(manifest_plist_path)
        return bool(plist.get("IsEncrypted", False))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def unlock(self) -> bool:
        """
        Derive the passphrase key from *self.password* and unwrap every
        class key in the keybag.

        Returns ``True`` on success.  Raises :class:`ValueError` if the
        password is wrong (key-unwrap integrity check fails).

        Key-derivation steps
        --------------------
        1. **Single PBKDF2** (all iOS versions):
           ``PBKDF2-HMAC-SHA1(password, salt, iterations)`` → 32-byte key.
        2. **Double PBKDF2** (iOS 10.2+, when ``dp_iterations > 0``):
           a. ``PBKDF2-HMAC-SHA256(password, dp_salt, dp_iterations)``
              → 32-byte intermediate key.
           b. ``HMAC-SHA256(intermediate_key, IOS_102_HMAC_FIXED_STRING)``
              → 32-byte final key.
           The final key from (2) is used *instead of* the key from (1).
        """
        if self.keybag is None:
            raise RuntimeError(
                "Keybag not loaded — was Manifest.plist parsed correctly?"
            )

        kb = self.keybag

        # --- Step 1: single PBKDF2 (legacy, or fallback) ----------------
        derived_key = hashlib.pbkdf2_hmac(
            "sha1",
            self.password.encode("utf-8"),
            kb.salt,
            kb.iterations,
            dklen=32,
        )
        logger.debug(
            "PBKDF2-HMAC-SHA1: iterations=%d, derived key length=%d",
            kb.iterations,
            len(derived_key),
        )

        # --- Step 2: double PBKDF2 for iOS 10.2+ ------------------------
        if kb.dp_iterations > 0 and kb.dp_salt:
            logger.info(
                "iOS 10.2+ double-protection detected "
                "(dp_iterations=%d).  This may take a while …",
                kb.dp_iterations,
            )
            # 2a. PBKDF2-HMAC-SHA256 with high iteration count
            intermediate = hashlib.pbkdf2_hmac(
                "sha256",
                self.password.encode("utf-8"),
                kb.dp_salt,
                kb.dp_iterations,
                dklen=32,
            )
            # 2b. HMAC-SHA256 with a fixed "purpose" string
            derived_key = hmac.new(
                intermediate, IOS_102_HMAC_FIXED_STRING, hashlib.sha256
            ).digest()
            logger.debug("Double-protection key derived successfully.")

        # --- Step 3: unwrap each class key with the derived key ----------
        unwrapped_count = 0
        for cls_id, ck in kb.class_keys.items():
            if not ck.wrapped_key:
                logger.debug("Class %d has no wrapped key, skipping.", cls_id)
                continue
            try:
                ck.unwrapped_key = _aes_unwrap_key(derived_key, ck.wrapped_key)
                unwrapped_count += 1
                logger.debug("Unwrapped class key %d.", cls_id)
            except ValueError:
                # If *any* key fails to unwrap the password is almost
                # certainly wrong — but try the rest for diagnostics.
                logger.error(
                    "Failed to unwrap class key %d.  The backup password "
                    "is most likely incorrect.",
                    cls_id,
                )
                raise ValueError(
                    f"Wrong backup password — could not unwrap class key "
                    f"{cls_id}.  Please double-check the password you set "
                    f"in iTunes / Finder."
                )

        logger.info("Successfully unwrapped %d class keys.", unwrapped_count)
        self._unlocked = True
        return True

    def decrypt_manifest_db(self) -> Path:
        """
        Decrypt ``Manifest.db`` and write the plaintext to
        ``Manifest.db.decrypted`` next to the original.

        Returns the :class:`Path` to the decrypted database.

        The manifest key and its protection class come from
        ``Manifest.plist → ManifestKey``.  The first four bytes of
        ManifestKey encode the protection class (little-endian uint32);
        the rest is the wrapped per-file key.
        """
        self._ensure_unlocked()

        encrypted_db_path = self.backup_dir / "Manifest.db"
        if not encrypted_db_path.exists():
            raise FileNotFoundError(
                f"Manifest.db not found in {self.backup_dir}"
            )

        encrypted_data = encrypted_db_path.read_bytes()

        # Unwrap the manifest key with the appropriate class key.
        manifest_file_key = self._unwrap_file_key(
            self.manifest_key, self.manifest_class
        )

        # Decrypt Manifest.db — it is AES-256-CBC with a zero IV.
        decrypted = _aes_decrypt_cbc(
            manifest_file_key,
            iv=b"\x00" * 16,
            data=encrypted_data,
        )
        decrypted = _strip_pkcs7_padding(decrypted)

        output_path = self.backup_dir / "Manifest.db.decrypted"
        output_path.write_bytes(decrypted)
        logger.info("Decrypted Manifest.db → %s", output_path)
        return output_path

    def decrypt_file(
        self, file_data: bytes, protection_class: int
    ) -> bytes:
        """
        Decrypt a single backup file's data.

        Parameters
        ----------
        file_data : bytes
            Raw (encrypted) bytes of the file as stored in the backup.
        protection_class : int
            The file's protection class (from ``Manifest.db → Files``).

        Returns
        -------
        bytes
            The decrypted file contents.

        Notes
        -----
        Each file is encrypted with AES-256-CBC using a **per-file key**.
        The per-file key is derived by computing
        ``SHA1(class_key + protection_class_as_be_uint32)`` and taking
        the first 32 bytes (with appropriate padding/truncation).

        .. warning::
           The per-file key derivation described above is a simplification.
           Real backups store the wrapped per-file key *inside* the manifest
           record for each file.  A full implementation must unwrap that key
           using the class key.

        TODO
        ----
        * Implement full per-file key unwrapping from manifest records.
        * Handle files whose protection class key was not unwrapped.
        * For a complete alternative, consider using ``iphone_backup_extractor``
          (https://github.com/KnugiHK/iphone_backup_extractor).
        """
        self._ensure_unlocked()

        if self.keybag is None:
            raise RuntimeError("Keybag not loaded.")

        ck = self.keybag.class_keys.get(protection_class)
        if ck is None or not ck.unwrapped_key:
            raise ValueError(
                f"No unwrapped key for protection class {protection_class}.  "
                f"Was unlock() called successfully?"
            )

        # TODO: In a production implementation the per-file wrapped key
        #       should be read from the manifest record and unwrapped here.
        #       The fallback below uses the class key directly with a zero IV,
        #       which works for Manifest.db but is **not** correct for
        #       arbitrary files.  See the iphone_backup_extractor project
        #       for the full algorithm.
        raise NotImplementedError(
            "Full per-file decryption is not yet implemented.  "
            "Each file's manifest record contains a wrapped per-file key "
            "that must be unwrapped with the class key before decryption.  "
            "For now, use 'iphone_backup_extractor' or a similar tool to "
            "extract individual files from encrypted backups.\n"
            "  pip install iphone-backup-extractor\n"
            "  https://github.com/KnugiHK/iphone_backup_extractor"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_unlocked(self) -> None:
        if not self._unlocked:
            raise RuntimeError(
                "Backup is still locked.  Call unlock() with the correct "
                "password first."
            )

    def _load_manifest_plist(self) -> dict:
        """Read and parse ``Manifest.plist`` from the backup directory."""
        path = self.backup_dir / "Manifest.plist"
        if not path.exists():
            raise FileNotFoundError(
                f"Manifest.plist not found in {self.backup_dir}.  "
                f"Is this a valid iTunes backup directory?"
            )
        return _read_plist(path)

    def _parse_keybag_and_manifest_key(self) -> None:
        """
        Extract the keybag blob and ManifestKey from the parsed plist.

        ``BackupKeyBag`` is a raw ``bytes`` blob inside ``Manifest.plist``.
        ``ManifestKey`` is ``<4-byte LE protection class> + <wrapped key>``.
        """
        plist = self._manifest_plist

        keybag_data = plist.get("BackupKeyBag")
        if keybag_data is None:
            raise ValueError(
                "BackupKeyBag not found in Manifest.plist.  "
                "The backup may not be encrypted."
            )
        if isinstance(keybag_data, bytes):
            self.keybag = Keybag.from_bytes(keybag_data)
        else:
            raise TypeError(
                f"Unexpected BackupKeyBag type: {type(keybag_data)}"
            )

        manifest_key_raw = plist.get("ManifestKey")
        if manifest_key_raw is None:
            raise ValueError("ManifestKey not found in Manifest.plist.")
        if not isinstance(manifest_key_raw, bytes) or len(manifest_key_raw) < 8:
            raise ValueError(
                f"ManifestKey has unexpected format (length={len(manifest_key_raw)})."
            )

        # First 4 bytes = protection class (little-endian uint32)
        self.manifest_class = struct.unpack("<I", manifest_key_raw[:4])[0]
        self.manifest_key = manifest_key_raw[4:]
        logger.debug(
            "ManifestKey: protection_class=%d, key_length=%d",
            self.manifest_class,
            len(self.manifest_key),
        )

    def _unwrap_file_key(
        self, wrapped_key: bytes, protection_class: int
    ) -> bytes:
        """
        Unwrap a per-file key using the class key for *protection_class*.

        Returns the raw 32-byte AES key.
        """
        if self.keybag is None:
            raise RuntimeError("Keybag not loaded.")

        ck = self.keybag.class_keys.get(protection_class)
        if ck is None:
            raise ValueError(
                f"Protection class {protection_class} not found in keybag."
            )
        if not ck.unwrapped_key:
            raise ValueError(
                f"Class key {protection_class} has not been unwrapped.  "
                f"Call unlock() first."
            )

        return _aes_unwrap_key(ck.unwrapped_key, wrapped_key)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _read_plist(path: Path) -> dict:
    """
    Read a plist file, trying ``plistlib`` first, then ``biplist``.

    Apple's binary plists occasionally use features that stdlib
    ``plistlib`` chokes on (e.g. UID types).  ``biplist`` handles those.
    """
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except Exception:
        if _HAS_BIPLIST:
            logger.debug(
                "plistlib failed on %s, falling back to biplist.", path
            )
            return biplist.readPlist(path)
        raise
