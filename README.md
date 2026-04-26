# WhatsApp iPhone → OneDrive Backup

Back up **WhatsApp data only** from your iPhone to OneDrive (instead of iCloud), and selectively restore it later — without touching the rest of your phone.

---

## Table of Contents

- [Why this tool?](#why-this-tool)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Part 1 — One-Time Setup](#part-1--one-time-setup)
  - [Step 1: Install iTunes](#step-1-install-itunes)
  - [Step 2: Configure your iPhone backup](#step-2-configure-your-iphone-backup)
  - [Step 3: Verify OneDrive is signed in](#step-3-verify-onedrive-is-signed-in)
  - [Step 4: Install this tool](#step-4-install-this-tool)
  - [Step 5: Run the setup wizard](#step-5-run-the-setup-wizard)
  - [Step 6: Schedule automatic backups (optional)](#step-6-schedule-automatic-backups-optional)
- [Part 2 — Daily Usage](#part-2--daily-usage)
- [Part 3 — Restoring WhatsApp](#part-3--restoring-whatsapp)
- [Command Reference](#command-reference)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Limitations & Honest Caveats](#limitations--honest-caveats)
- [Project Structure](#project-structure)
- [License](#license)

---

## Why this tool?

WhatsApp on iPhone normally only backs up to **iCloud**. If you:
- Are running out of iCloud storage
- Prefer OneDrive (e.g., you have a Microsoft 365 subscription with 1 TB)
- Want a **local copy** of your chats that you control
- Don't trust cloud-only backups

…this tool gives you an automated WhatsApp-only backup pipeline to OneDrive.

## How it works

```
BACKUP (automatic):
  iPhone ──Wi-Fi sync──> iTunes Backup on PC ──> Extract WhatsApp only ──> OneDrive folder ──> Cloud

RESTORE (manual, when needed):
  OneDrive ──> Download WhatsApp data
  iPhone ──> Take fresh iTunes backup
  Tool injects WhatsApp data into the fresh backup
  iTunes restores ──> Phone stays current, only WhatsApp is replaced
```

The key insight: we never restore an old full-phone backup. Instead, we take a *current* backup, swap in the WhatsApp data we want, and restore that. This is the same technique commercial tools like iMazing use.

## Requirements

- **Windows 10 or 11**
- **Python 3.10 or newer** ([download](https://www.python.org/downloads/))
- **iTunes** from the Microsoft Store (free)
- **OneDrive desktop app** (already installed and signed in on Windows)
- **iPhone** with WhatsApp installed
- A **USB cable** for the first-time iPhone connection
- Your iPhone and PC on the **same Wi-Fi network**

---

## Part 1 — One-Time Setup

### Step 1: Install iTunes

1. Open the **Microsoft Store** app on Windows
2. Search for **"iTunes"** and click **Install** (it's free)
3. Wait for installation to finish, then launch iTunes once and accept the license

> **Alternative:** You can use the newer **Apple Devices** app from the Microsoft Store. The tool supports both backup locations.

### Step 2: Configure your iPhone backup

1. Connect your iPhone to your PC with a USB cable
2. On your iPhone, tap **Trust** when prompted
3. In iTunes, click the **iPhone icon** in the top-left
4. Click **Summary** in the left sidebar
5. Under **Backups**:
   - Select **"This computer"** (NOT iCloud)
   - ✅ Check **"Encrypt local backup"** — *this is required* (only encrypted backups include WhatsApp data)
   - Set a **backup password** when prompted — **write it down somewhere safe**, you'll need it for this tool
   - ✅ Check **"Sync with this iPhone over Wi-Fi"** — so future backups don't need a cable
6. Click **Apply**, then **Back Up Now**
7. Wait for the backup to finish (5–30 minutes depending on phone size)

> 💡 **From now on**, your iPhone will back up automatically whenever it's:
> - **Charging**
> - On the **same Wi-Fi network** as your PC
> - And iTunes is **running** on the PC

### Step 3: Verify OneDrive is signed in

1. Click the **OneDrive cloud icon** in your Windows system tray (bottom-right)
2. Make sure you're signed in
3. Note your OneDrive folder location (usually `C:\Users\<you>\OneDrive`)

### Step 4: Install this tool

Open **PowerShell** (or Windows Terminal) and run:

```powershell
cd C:\Users\<your-username>\source\repos
git clone https://github.com/rambo108/whatsapp-onedrive-backup.git
cd whatsapp-onedrive-backup
pip install -r requirements.txt
```

Verify it installed:
```powershell
python main.py --help
```
You should see the list of available commands.

### Step 5: Run the setup wizard

```powershell
python main.py setup
```

The wizard will:
1. Ask for your **iTunes backup password** (the one you set in Step 2) — stored securely in **Windows Credential Manager**, never written to disk in plain text
2. **Auto-detect** your OneDrive folder and confirm with you
3. Ask how many backup versions to keep (default: 30)
4. Save your preferences to `config.toml`
5. List the iTunes backups it can see, to verify the connection works

### Step 6: Schedule automatic backups (optional but recommended)

To make backups run automatically every hour:

```powershell
python -c "from scheduler.setup import SchedulerSetup; s = SchedulerSetup(); s.install_backup_task(interval_minutes=60); s.enable_itunes_autostart()"
```

This will:
- Create a Windows Scheduled Task named **"WhatsAppOneDriveBackup"** that runs `main.py backup` every 60 minutes
- Add **iTunes to Windows startup** so it's always running for Wi-Fi sync

To verify:
```powershell
python -c "from scheduler.setup import SchedulerSetup; print(SchedulerSetup().get_task_status())"
```

---

## Part 2 — Daily Usage

**There's nothing to do daily.** Once set up, the flow runs unattended:

| Trigger | What happens |
|---------|--------------|
| You plug in your iPhone to charge (on home Wi-Fi) | iTunes backs it up automatically |
| Every 60 min | Scheduled task runs `python main.py backup` |
| The backup task runs | Extracts WhatsApp from latest iTunes backup → copies to OneDrive folder → OneDrive desktop app uploads to cloud |

You can manually trigger or check things anytime:

```powershell
python main.py status        # Show current state
python main.py backup        # Run a backup right now
python main.py list          # See all backups stored on OneDrive
```

---

## Part 3 — Restoring WhatsApp

> ⚠️ **Read this whole section before starting a restore.** The selective restore is experimental — back up your phone before trying it.

### When to restore
- You got a new iPhone and want your old WhatsApp chats
- Your WhatsApp data got corrupted or accidentally deleted
- You want to roll back to an earlier WhatsApp state

### Restore steps

1. **Make sure your iPhone is connected** (USB or same Wi-Fi as PC)

2. **List your available backups** to pick one:
   ```powershell
   python main.py list
   ```
   Output looks like:
   ```
   Name                              Date              Size       Files
   whatsapp_2026-04-25_140000        2026-04-25 14:00  2.4 GB     1842
   whatsapp_2026-04-24_140000        2026-04-24 14:00  2.4 GB     1839
   ```

3. **Take a fresh iTunes backup of your iPhone in its CURRENT state** (this is what stays after restore — only WhatsApp will change):
   - Open iTunes → iPhone → Summary → **Back Up Now**
   - Wait for it to finish

4. **Run the restore:**
   ```powershell
   python main.py restore whatsapp_2026-04-25_140000
   ```
   The tool will:
   - Download the WhatsApp data from OneDrive
   - Find your fresh iTunes backup
   - **Make a safety copy** of the fresh backup
   - Inject the WhatsApp data into it
   - Verify the injection
   - Print final instructions

5. **Restore via iTunes:**
   - In iTunes → iPhone → Summary → **Restore Backup…**
   - Select the modified backup (it will have today's date)
   - Enter your backup password
   - Wait for restore to finish (your iPhone will reboot)

6. **Open WhatsApp** on your iPhone:
   - It may ask you to verify your phone number
   - Your chats and media will be restored from the backup you selected

### If something goes wrong
The safety copy created in step 4 lets you restore your phone back to its current state. The original fresh backup is also untouched.

---

## Command Reference

| Command | What it does |
|---------|-------------|
| `python main.py setup` | Interactive setup wizard (run once) |
| `python main.py backup` | Run a backup cycle now |
| `python main.py list` | List all backups stored on OneDrive |
| `python main.py status` | Show iTunes backup status, OneDrive sync state, retention info |
| `python main.py restore <name>` | Restore a specific WhatsApp backup |
| `python main.py --help` | Show help |
| `python main.py -v <command>` | Run with verbose/debug logging |

---

## Configuration

Settings live in `config.toml`. The setup wizard creates this for you, but you can edit it manually:

```toml
[backup]
itunes_backup_path = ""  # Leave empty for auto-detect

[onedrive]
sync_folder = "~/OneDrive/WhatsApp-Backups"

[retention]
keep_versions = 30  # Keep last 30 backups, delete older

[scheduler]
check_interval_minutes = 60
auto_launch_itunes = true
```

The **iTunes backup password** is NOT stored in this file. It's stored in Windows Credential Manager (service: `whatsapp-onedrive-backup`, account: `itunes-backup`).

---

## Troubleshooting

**"No iTunes backup found"**
- Make sure you've done at least one backup with iTunes ("Back Up Now")
- Check the backup folder exists: `%APPDATA%\Apple Computer\MobileSync\Backup\`
- Or for the Apple Devices app: `%USERPROFILE%\Apple\MobileSync\Backup\`

**"Backup is encrypted but no password found"**
- Run `python main.py setup` again to re-enter your password
- The password is the one you set in iTunes when enabling encryption

**"OneDrive folder not found"**
- Make sure the OneDrive desktop app is installed and signed in
- Right-click the OneDrive system tray icon → Settings → Account → check folder location
- Edit `config.toml` to set `sync_folder` manually

**"OneDrive is not running"**
- Click the OneDrive icon in your system tray to start it
- Or set it to launch at startup (Windows Settings → Apps → Startup)

**"Scheduled task not running"**
- Open **Task Scheduler** (search in Start menu)
- Look for **"WhatsAppOneDriveBackup"**
- Check the **Last Run Result** column for errors
- Make sure your user account is logged in (the task only runs while logged in)

**WhatsApp doesn't see the restored data**
- After restoring via iTunes, fully close WhatsApp (swipe up from bottom, swipe up on WhatsApp)
- Reopen — WhatsApp will detect and load the restored database
- You may need to verify your phone number again

---

## Limitations & Honest Caveats

- **Wi-Fi sync requires iTunes to be running** on the PC. Use Step 6 to auto-launch it.
- **Encryption is mandatory** for WhatsApp data to be in the backup. There's no way around this Apple restriction.
- **Per-file decryption of encrypted backups** is not fully implemented in `backup/decrypt.py` — only the manifest is decrypted. If iTunes itself can read your backup (which it can if you have the password), you don't need this code path. But for fully decrypting blobs without iTunes, consider [`iphone_backup_decrypt`](https://github.com/jsharkey13/iphone_backup_decrypt).
- **Selective restore is experimental.** It works by modifying the backup manifest — same technique iMazing uses, but Apple may break it in future iOS versions. Always keep the safety copy.
- **Not real-time.** This is periodic backup, not continuous sync.
- **WhatsApp may re-verify your phone number** after restore. This is normal and harmless.
- **End-to-end encrypted backup** (WhatsApp's own E2E feature for cloud backups) is not used here — your data is protected by your iTunes backup password instead.

---

## Project Structure

```
whatsapp-onedrive-backup/
├── main.py                  # CLI entry point
├── config.toml              # User configuration
├── requirements.txt         # Python dependencies
├── backup/
│   ├── finder.py            # Locate iTunes backups on disk
│   ├── extractor.py         # Extract WhatsApp files from backup
│   └── decrypt.py           # Encrypted backup support
├── onedrive/
│   └── sync.py              # Copy to OneDrive sync folder
├── restore/
│   └── injector.py          # Inject WhatsApp data into fresh backup
├── scheduler/
│   └── setup.py             # Windows Task Scheduler integration
├── README.md
└── LICENSE
```

---

## License

[MIT](LICENSE) — use it, modify it, share it. No warranty.

---

## Contributing / Issues

Found a bug? Open an issue at https://github.com/rambo108/whatsapp-onedrive-backup/issues

PRs welcome — especially for:
- Per-file blob decryption in `backup/decrypt.py`
- Test coverage
- macOS support (the extractor logic mostly works, but paths and OneDrive integration need adapting)
