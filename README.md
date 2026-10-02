# Windows Update Control

> **A tool to fully disable or restore Windows Update on Windows 10 and 11.**

---

## 📋 About

**Windows Update Control** is a Python utility with a modern PyQt6 GUI that lets you completely disable or restore Windows Update with a single click. It goes far beyond simply stopping the update service — it applies Group Policy blocks, neutralizes all related scheduled tasks, and removes service recovery actions so nothing can auto-restart updates behind your back.

All original settings are backed up to the registry before any changes are made, so restoring is safe and accurate.

---

## ✨ Features

- 🔴 **Disable** all Windows Update services (`wuauserv`, `UsoSvc`, `WaaSMedicSvc`, `BITS`, `DoSvc`)
- 🛡️ **Group Policy block** — points the update client at a dead local WSUS server so scans can't reach Microsoft
- 📅 **Scheduled tasks** — disables `WindowsUpdate`, `UpdateOrchestrator`, and `WaaSMedic` tasks via a SYSTEM-level helper
- 💾 **Automatic backup** — saves your original service start types, recovery actions, and policy values to `HKLM\SOFTWARE\WUControl` before making any changes
- ✅ **One-click restore** — re-enables everything exactly as it was before disabling
- 🪟 **Dark modern UI** — built with PyQt6 (Fusion style), shows real-time status of every service and policy
- 🔐 **UAC elevation** — automatically requests Administrator rights on launch

---

## 🖼️ What It Does (Technical Overview)

When **disabling**, the tool:
1. Backs up current service and policy settings to the registry
2. Applies Group Policy values (`NoAutoUpdate`, fake WSUS server URL, etc.)
3. Sets all 5 update services to `Disabled` and removes their failure recovery actions (uses `SeBackupPrivilege`/`SeRestorePrivilege` to bypass TrustedInstaller ACL on protected keys like `WaaSMedicSvc`)
4. Stops all running services
5. Disables related scheduled tasks by running a temporary SYSTEM-level PowerShell task

When **enabling**, every step above is reversed using the saved backup.

---

## 🚀 Running from Source (Python)

### Requirements

- **Python 3.11+**
- **Windows 10 or Windows 11**
- PyQt6

### Installation

```bash
# Clone the repository
git clone https://github.com/Whoami304/Disable-Windows-Update.git
cd Disable-Windows-Update

# (Optional) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Run

```bash
python main.py
```

> ⚠️ The application will automatically prompt for **Administrator (UAC)** elevation on startup. Changes require admin rights. You can still view the current status without elevation.

---

## 📦 Running the Compiled Executable

A pre-compiled Windows executable is provided — **no Python installation needed**.

1. Download **`WindowsUpdateControl.exe`** from the [Releases](../../releases) page
2. Right-click → **Run as Administrator** (or just double-click — UAC will prompt automatically)
3. Use the **Disable Updates** or **Enable Updates** buttons

> ✅ The `.exe` was built with PyInstaller and is fully self-contained.

---

## 📁 File Structure

```
Disable-Windows-Update/
├── Disable_Update.py      # Backend — all logic for disabling/enabling Windows Update
├── main.py                # Frontend — PyQt6 GUI application
├── requirements.txt       # Python dependencies (PyQt6>=6.6)
└── WindowsUpdateControl.exe  # Pre-compiled standalone executable
```

---

## ⚠️ Disclaimer

This tool modifies system services, registry keys, Group Policy, and scheduled tasks. Use at your own risk. Always create a system restore point before making changes. The backup/restore feature is included to make reverting easy, but the author is not responsible for any unintended system behavior.

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
