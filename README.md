# Temp Monitor

CPU temperature monitoring across machines. Each machine runs a lightweight
**companion** agent that reads sensors via LibreHardwareMonitor and reports
them to a central **hub** (Flask + Socket.IO) for live charts and history.

- Hub: `app.py` (served via `wsgi.py`), live at https://temp.arkeanos.net
- Companion agent: `companion.py`
- Installer: `install.ps1`

## Installing the companion agent

Run on the Windows machine you want to monitor. The installer needs an
elevated (admin) PowerShell, since LibreHardwareMonitor needs admin rights to
read sensors and the agent runs via scheduled tasks.

**From the web (recommended):**

```powershell
irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1 | iex
```

**With parameters** (`iex` alone can't take arguments, so invoke the fetched
script as a scriptblock instead):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1))) -Uninstall
```

Supported parameters: `-Uninstall`, `-InstallDir <path>` (default
`C:\Program Files\TempMonitor`), `-Port <port>` (default `8085`, used by
LibreHardwareMonitor's web server).

**From a local clone:**

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```

The installer will elevate itself automatically if not already run as admin.

### What it installs

- Python 3 (via `winget`, if missing) + the `requests` package
- LibreHardwareMonitor (latest GitHub release), configured to run its web
  server on the configured port
- PawnIO (skipped if the `PawnIO` service already exists) -- the kernel
  driver LibreHardwareMonitor needs to read sensors on modern Windows
- `companion.py`, pulled from `main`
- Two scheduled tasks (run at logon, admin rights): LibreHardwareMonitor,
  then the companion agent 30s later

### Uninstalling

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```

Removes the scheduled tasks, stops the running processes, and deletes the
install directory. Python itself is left alone.

## Self-updates

`companion.py` checks `VERSION` against the copy on `main` every start, and
weekly thereafter while running, swapping itself for the newer version when
found. No separate version file is needed — bump the `VERSION` constant near
the top of `companion.py` on every push to `main`, or nothing will update.

## Hub

`app.py` receives reports at `POST /api/report`, and serves live/historical
views at `/` and `/history`. Data is persisted to `logs/temp_v2.db`
(SQLite) with optional CSV archiving; rotated log files also live under
`logs/`. Run it via `wsgi.py`, or directly with:

```powershell
python app.py
```
