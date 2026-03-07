# Tesla Wall Charger Tracker

Python web application that polls a Tesla Wall Connector API on your local network, stores charging sessions in SQLite, and lets you manually label sessions with the charged vehicle.

## Features
- Polls `/api/1/vitals` from Tesla Wall Connector
- Stores sessions only in SQLite (no telemetry history table)
- Auto-detects charging sessions based on `contactor_closed`
- Writes session rows to DB only when a session closes
- Computes session energy from meter delta (`energy_wh`-style fields) when available, with power*time fallback
- Estimates session price from PG&E TOU plans (EV2-A, EV-B, E-ELEC)
- Live telemetry in UI directly from API (including current meter energy)
- Filters (vehicle/date), totals, and CSV export

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration
Environment variables:
- `TWC_BASE_URL` (default: `http://192.168.1.167`)
- `POLL_INTERVAL_SECONDS` (default: `15`)
- `TWC_TIMEOUT_SECONDS` (default: `4`)
- `APP_DB_PATH` (default: `./tesla_wall_charger.db`)
- `APP_TIMEZONE` (default: `America/Los_Angeles`)
- `APP_RATE_PLAN` (default: `EV2-A`; options: `EV2-A`, `EV-B`, `E-ELEC`)

Example:
```powershell
$env:TWC_BASE_URL='http://192.168.1.50'
$env:POLL_INTERVAL_SECONDS='10'
$env:APP_TIMEZONE='America/Los_Angeles'
$env:APP_RATE_PLAN='EV2-A'
```

## Run
```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Open: `http://localhost:8080`

## Notes
- Wall Connector API fields vary by firmware. This app maps common keys and falls back where possible.
- PG&E TOU pricing is modeled from tariff schedules and used as an energy-only estimate.
- On Windows, timezone names like `America/Los_Angeles` require `tzdata` installed from `requirements.txt`.
- If live telemetry is empty, test API manually:
```powershell
Invoke-RestMethod http://YOUR_WALL_CONNECTOR_IP/api/1/vitals
```
