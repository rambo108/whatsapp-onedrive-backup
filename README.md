# WhatsApp iPhone → OneDrive Backup

Back up **WhatsApp data only** from your iPhone to OneDrive (instead of iCloud), and selectively restore it without affecting the rest of your phone.

## Why?

WhatsApp on iPhone normally only backs up to iCloud. If you're running out of iCloud storage, prefer OneDrive, or want a local copy you control, this tool is for you.

## How It Works

```
BACKUP:  iPhone → iTunes Backup → Extract WhatsApp only → OneDrive folder → Cloud
RESTORE: OneDrive → Take fresh backup → Inject WhatsApp data → Restore via iTunes
```

The selective restore means **your phone stays current** — only WhatsApp data is replaced.

## Requirements

- Windows 10/11
- Python 3.10+
- iTunes (from Microsoft Store) **or** Apple Devices app
- OneDrive desktop app (already on Windows)
- iPhone with WhatsApp

## Setup (One-Time)

### 1. Set up iTunes backup
1. Install iTunes from the Microsoft Store
2. Connect iPhone via USB, trust the computer
3. In iTunes → iPhone → Summary:
   - Select **"This computer"**
   - Check **"Encrypt local backup"** *(required to include WhatsApp data)*
   - Set a backup password
   - Check **"Sync with this iPhone over Wi-Fi"**
4. Click **Back Up Now**

### 2. Install this tool
```powershell
git clone https://github.com/<you>/whatsapp-onedrive-backup.git
cd whatsapp-onedrive-backup
pip install -r requirements.txt
python main.py setup
```

The setup wizard will:
- Ask for your iTunes backup password (stored in Windows Credential Manager)
- Auto-detect your OneDrive folder
- Configure automatic scheduling
- Run a test backup

## Daily Usage

After setup, everything is automatic:
- iTunes backs up your iPhone over Wi-Fi when it's charging
- A scheduled task extracts WhatsApp data and copies it to OneDrive
- OneDrive syncs to the cloud

## Manual Commands

```powershell
python main.py backup       # Run a backup now
python main.py list         # List backups stored on OneDrive
python main.py status       # Show current state
python main.py restore <backup-name>   # Restore a specific backup
```

## Restore Process

1. Run `python main.py list` to see available backups
2. Run `python main.py restore <backup-name>`
3. The tool will guide you through:
   - Making a fresh iTunes backup of your current phone
   - Injecting the WhatsApp data into that fresh backup
   - Restoring via iTunes (only WhatsApp changes — phone stays current)

## Project Structure

```
whatsapp-onedrive-backup/
├── main.py                  # CLI entry point
├── config.toml              # Configuration
├── backup/
│   ├── finder.py            # Locate iTunes backups
│   ├── extractor.py         # Extract WhatsApp files
│   └── decrypt.py           # Encrypted backup support
├── onedrive/
│   └── sync.py              # OneDrive folder sync
├── restore/
│   └── injector.py          # Selective WhatsApp restore
└── scheduler/
    └── setup.py             # Windows Task Scheduler integration
```

## Limitations

- iPhone must be on same Wi-Fi as PC (or USB) to back up
- iTunes encryption password is required (encryption must be enabled)
- Selective restore is experimental — same technique used by tools like iMazing
- After restore, WhatsApp may re-verify your phone number

## License

MIT — see [LICENSE](LICENSE)
