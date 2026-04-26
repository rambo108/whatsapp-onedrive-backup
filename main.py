"""WhatsApp OneDrive Backup — CLI entry point.

Subcommands:
    setup    Interactive setup wizard.
    backup   Run a backup cycle (iTunes -> WhatsApp extract -> OneDrive).
    list     List backups available on OneDrive.
    restore  Restore a WhatsApp dataset from OneDrive into a fresh iTunes backup.
    status   Show current state of backups and sync.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

import keyring

from backup.finder import BackupFinder
from backup.extractor import WhatsAppExtractor, EncryptedBackupError
from backup.decrypt import BackupDecryptor
from onedrive.sync import OneDriveSync
from restore.injector import RestoreInjector


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEYRING_SERVICE = "whatsapp-onedrive-backup"
KEYRING_USERNAME = "itunes-backup"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"

log = logging.getLogger("wa-onedrive")


# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"


def _c(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{C.RESET}"


def info(msg: str) -> None:
    print(_c("• ", C.CYAN) + msg)


def success(msg: str) -> None:
    print(_c("✓ ", C.GREEN) + msg)


def warn(msg: str) -> None:
    print(_c("! ", C.YELLOW) + msg)


def error(msg: str) -> None:
    print(_c("✗ ", C.RED) + msg, file=sys.stderr)


def header(msg: str) -> None:
    print()
    print(_c(msg, C.BOLD + C.BLUE))
    print(_c("─" * len(msg), C.DIM))


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("rb") as f:
        return tomllib.load(f)


def save_config(config_path: Path, config: dict[str, Any]) -> None:
    """Write config.toml. Minimal serializer — preserves only the sections we manage."""
    lines: list[str] = []
    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def get_password() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)


def set_password(password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, password)


# ---------------------------------------------------------------------------
# Subcommand: setup
# ---------------------------------------------------------------------------

def cmd_setup(args: argparse.Namespace) -> int:
    header("WhatsApp OneDrive Backup — Setup Wizard")

    config_path: Path = args.config
    config: dict[str, Any] = {}
    if config_path.exists():
        try:
            config = load_config(config_path)
        except Exception as exc:
            warn(f"Could not parse existing config ({exc}); starting fresh.")

    # 1. Encryption password
    info("iTunes encrypted backups need your backup password to be decrypted.")
    info("The password will be stored securely in Windows Credential Manager.")
    existing = get_password()
    if existing:
        warn("A password is already stored. Press Enter to keep it, or type a new one.")
    password = getpass.getpass("iTunes backup password: ")
    if password:
        set_password(password)
        success("Password saved to Windows Credential Manager.")
    elif not existing:
        warn("No password set. Encrypted backups will fail until you run setup again.")

    # 2. Auto-detect OneDrive
    header("OneDrive detection")
    sync = OneDriveSync()
    detected = sync.find_onedrive_folder()
    if detected:
        info(f"Detected OneDrive folder: {detected}")
        answer = input(f"Use this folder? [Y/n]: ").strip().lower()
        onedrive_root = Path(detected) if answer in ("", "y", "yes") else Path(input("OneDrive folder path: ").strip())
    else:
        warn("Could not auto-detect OneDrive folder.")
        onedrive_root = Path(input("OneDrive folder path: ").strip())

    backup_subfolder = input("Subfolder for WhatsApp backups [WhatsAppBackups]: ").strip() or "WhatsAppBackups"
    onedrive_folder = onedrive_root / backup_subfolder
    onedrive_folder.mkdir(parents=True, exist_ok=True)
    success(f"OneDrive backup folder: {onedrive_folder}")

    # 3. Retention
    header("Retention policy")
    keep_default = config.get("retention", {}).get("keep_versions", 10)
    keep_raw = input(f"How many backup versions to keep? [{keep_default}]: ").strip()
    keep_versions = int(keep_raw) if keep_raw else int(keep_default)

    # 4. Save config
    config.setdefault("backup", {})
    config.setdefault("onedrive", {})
    config.setdefault("retention", {})
    config.setdefault("scheduler", {})
    config["onedrive"]["folder"] = str(onedrive_folder)
    config["onedrive"]["root"] = str(onedrive_root)
    config["retention"]["keep_versions"] = keep_versions
    save_config(config_path, config)
    success(f"Configuration written to {config_path}")

    # 5. Test: list iTunes backups
    header("Sanity check — iTunes backups")
    finder = BackupFinder()
    backups = finder.list_backups()
    if not backups:
        warn("No iTunes backups found. Make a backup in iTunes / Apple Devices first.")
    else:
        info(f"Found {len(backups)} iTunes backup(s):")
        for b in backups:
            enc = _c("[encrypted]", C.YELLOW) if b.encrypted else _c("[plain]", C.DIM)
            print(f"   • {b.device_name} (iOS {b.ios_version}) — {b.last_backup_date} {enc}")

    success("Setup complete.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: backup
# ---------------------------------------------------------------------------

def cmd_backup(args: argparse.Namespace) -> int:
    header("WhatsApp OneDrive Backup — Run")
    config = load_config(args.config)
    onedrive_folder = Path(config["onedrive"]["folder"])
    keep_versions = int(config.get("retention", {}).get("keep_versions", 10))

    finder = BackupFinder()
    latest = finder.get_latest_backup()
    if latest is None:
        error("No iTunes backup found. Connect your iPhone and back it up first.")
        return 2
    info(f"Latest iTunes backup: {latest.device_name} (iOS {latest.ios_version}) — {latest.last_backup_date}")

    backup_dir = Path(latest.path)

    # ------------------------------------------------------------------
    # FULL BACKUP MODE — copy the entire iTunes backup folder.
    # This gives a PROVEN restore path: just restore via iTunes from the copy.
    # Trade-off: backups are much larger (10-100 GB).
    # ------------------------------------------------------------------
    if getattr(args, "full", False):
        warn("Full backup mode: copying the ENTIRE iTunes backup folder.")
        warn("This may be large (10-100 GB) but provides a proven restore path via iTunes.")

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_name = f"full_{timestamp}"
        info(f"Syncing full backup to OneDrive as {backup_name}...")
        sync = OneDriveSync()
        sync.sync_to_onedrive(backup_dir, onedrive_folder, backup_name)
        success(f"Full backup saved to OneDrive: {onedrive_folder / backup_name}")
        info("To restore: download this folder, place it in iTunes' backup location,")
        info("then use iTunes 'Restore Backup' to restore your iPhone.")

        info(f"Applying retention policy (keep {keep_versions})...")
        sync.apply_retention(onedrive_folder, keep_versions)
        if not sync.is_onedrive_running():
            warn("OneDrive doesn't appear to be running — files won't sync until it starts.")
        return 0

    # ------------------------------------------------------------------
    # WHATSAPP-ONLY MODE (default) — extract only WhatsApp data.
    # Smaller backups, but restore requires iMazing or our experimental injector.
    # ------------------------------------------------------------------
    extractor = WhatsAppExtractor()

    # Decrypt manifest if needed
    if latest.encrypted or BackupDecryptor.is_encrypted(backup_dir):
        info("Backup is encrypted — unlocking with stored password...")
        password = get_password()
        if not password:
            error("No password in keyring. Run `setup` to store one.")
            return 2
        decryptor = BackupDecryptor(backup_dir, password)
        try:
            decryptor.unlock()
            decryptor.decrypt_manifest_db()
            success("Manifest decrypted.")
        except Exception as exc:
            error(f"Failed to decrypt backup: {exc}")
            return 2

    # Extract to temp dir
    with tempfile.TemporaryDirectory(prefix="wa-extract-") as tmp:
        tmp_dir = Path(tmp)
        info(f"Extracting WhatsApp files to {tmp_dir}...")
        try:
            result = extractor.extract(backup_dir, tmp_dir)
        except EncryptedBackupError:
            error("Encrypted backup — unable to extract WhatsApp files. Run `setup` to store password.")
            return 2
        except Exception as exc:
            error(f"Extraction failed: {exc}")
            return 1
        success(f"Extracted {getattr(result, 'file_count', '?')} files.")

        # Change detection
        info("Computing manifest and checking for changes...")
        new_manifest = extractor.compute_manifest()
        if not extractor.has_changes():
            success("No changes since last backup. Nothing to upload.")
            return 0

        # Sync to OneDrive
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_name = f"whatsapp_{timestamp}"
        info(f"Syncing to OneDrive as {backup_name}...")
        sync = OneDriveSync()
        sync.sync_to_onedrive(tmp_dir, onedrive_folder, backup_name)
        extractor.save_manifest()
        success(f"Backup saved to OneDrive: {onedrive_folder / backup_name}")

    # Retention
    info(f"Applying retention policy (keep {keep_versions})...")
    sync = OneDriveSync()
    sync.apply_retention(onedrive_folder, keep_versions)
    success("Retention applied.")

    if not sync.is_onedrive_running():
        warn("OneDrive doesn't appear to be running — files won't sync until it starts.")

    return 0


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    header("OneDrive backups")
    config = load_config(args.config)
    onedrive_folder = Path(config["onedrive"]["folder"])
    sync = OneDriveSync()
    backups = sync.list_backups(onedrive_folder)
    if not backups:
        warn(f"No backups found in {onedrive_folder}")
        return 0

    print(f"{'Name':<32} {'Date':<20} {'Files':>8} {'Size':>12}")
    print(_c("-" * 76, C.DIM))
    for b in backups:
        name = getattr(b, "name", str(b))
        date = getattr(b, "date", "")
        files = getattr(b, "file_count", "")
        size = getattr(b, "size", "")
        size_str = _format_size(size) if isinstance(size, (int, float)) else str(size)
        print(f"{name:<32} {str(date):<20} {str(files):>8} {size_str:>12}")
    return 0


def _format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


# ---------------------------------------------------------------------------
# Subcommand: restore
# ---------------------------------------------------------------------------

def cmd_restore(args: argparse.Namespace) -> int:
    header(f"Restore: {args.backup_name}")
    config = load_config(args.config)
    onedrive_folder = Path(config["onedrive"]["folder"])

    sync = OneDriveSync()
    with tempfile.TemporaryDirectory(prefix="wa-restore-") as tmp:
        tmp_dir = Path(tmp)
        info(f"Copying backup from OneDrive to {tmp_dir}...")
        try:
            sync.restore_from_onedrive(onedrive_folder, args.backup_name, tmp_dir)
        except Exception as exc:
            error(f"Failed to copy backup from OneDrive: {exc}")
            return 1
        success("WhatsApp data staged locally.")

        warn("Before continuing, make a FRESH unencrypted iTunes backup of the target iPhone.")
        warn("Open iTunes / Apple Devices, connect the phone, and click 'Back Up Now'.")
        input("Press Enter once the fresh iTunes backup is complete... ")

        finder = BackupFinder()
        fresh = finder.get_latest_backup()
        if fresh is None:
            error("No iTunes backup found after waiting.")
            return 2
        info(f"Using fresh backup: {fresh.device_name} — {fresh.last_backup_date}")

        injector = RestoreInjector()
        info("Validating backup...")
        if not injector.validate_backup():
            error("Backup validation failed.")
            return 1

        info("Creating safety copy of the fresh backup...")
        injector.create_backup_copy()

        info("Injecting WhatsApp data into the fresh backup...")
        try:
            result = injector.inject_whatsapp(Path(fresh.path), tmp_dir)
        except Exception as exc:
            error(f"Injection failed: {exc}")
            return 1
        success(f"Injected {getattr(result, 'file_count', '?')} files.")

        info("Verifying injection...")
        if not injector.verify_injection():
            error("Injection verification failed.")
            return 1
        success("Injection verified.")

    header("Next steps")
    print("1. Open iTunes / Apple Devices on this PC.")
    print("2. Connect the iPhone you want to restore.")
    print("3. Choose 'Restore Backup...' and select the most recent backup.")
    print("4. After restore, WhatsApp will detect the data on first launch.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    header("Status")
    config = load_config(args.config)
    onedrive_folder = Path(config["onedrive"]["folder"])
    keep_versions = int(config.get("retention", {}).get("keep_versions", 10))

    # iTunes
    finder = BackupFinder()
    latest = finder.get_latest_backup()
    if latest:
        info(f"Latest iTunes backup: {latest.device_name} (iOS {latest.ios_version})")
        print(f"    Path:    {latest.path}")
        print(f"    Date:    {latest.last_backup_date}")
        print(f"    UDID:    {latest.udid}")
        print(f"    Encrypted: {latest.encrypted}")
    else:
        warn("No iTunes backup found.")

    # OneDrive
    sync = OneDriveSync()
    backups = sync.list_backups(onedrive_folder)
    if backups:
        latest_od = backups[-1] if hasattr(backups[-1], "name") else backups[0]
        info(f"OneDrive backups: {len(backups)} (folder: {onedrive_folder})")
        print(f"    Latest:  {getattr(latest_od, 'name', '?')} @ {getattr(latest_od, 'date', '?')}")
    else:
        warn(f"No OneDrive backups in {onedrive_folder}")

    # Sync state
    if sync.is_onedrive_running():
        success("OneDrive is running.")
    else:
        warn("OneDrive is NOT running — new files won't sync until you start it.")

    info(f"Retention: keep {keep_versions} versions")

    # Keyring
    if get_password():
        success("iTunes backup password is stored in Windows Credential Manager.")
    else:
        warn("No iTunes backup password stored. Run `setup` if your backups are encrypted.")
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wa-onedrive",
        description="Back up your iPhone WhatsApp data to OneDrive via iTunes backups.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Run the interactive setup wizard.")
    p_backup = sub.add_parser("backup", help="Run a backup cycle.")
    p_backup.add_argument(
        "--full",
        action="store_true",
        help="Back up the ENTIRE iTunes backup folder (large but proven restore via iTunes). "
             "Default is WhatsApp-only (smaller, restore requires iMazing or experimental injector).",
    )
    sub.add_parser("list", help="List backups available on OneDrive.")
    p_restore = sub.add_parser("restore", help="Restore a backup from OneDrive.")
    p_restore.add_argument("backup_name", help="Name of the OneDrive backup to restore (see `list`).")
    sub.add_parser("status", help="Show current backup/sync status.")

    return parser


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    handlers = {
        "setup": cmd_setup,
        "backup": cmd_backup,
        "list": cmd_list,
        "restore": cmd_restore,
        "status": cmd_status,
    }
    handler = handlers[args.command]

    try:
        return handler(args)
    except FileNotFoundError as exc:
        error(str(exc))
        if args.command != "setup":
            warn("Run `python main.py setup` to create the configuration.")
        return 2
    except KeyboardInterrupt:
        error("Interrupted.")
        return 130
    except Exception as exc:
        log.exception("Unhandled error")
        error(f"{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
