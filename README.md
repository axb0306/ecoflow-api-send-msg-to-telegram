# EcoFlow Power Monitor → Telegram

Monitors an EcoFlow device (**The Farm Internet**, a River 3 Plus Wireless) via
the EcoFlow Developer API every 30 seconds. When grid/AC input power drops it
sends a Telegram alert; when power returns it sends a recovery message with the
**total outage duration** and the **current temperature**.

## How it works

- Polls `GET /iot-open/sign/device/quota/all` for the device, signed with
  HMAC-SHA256 (EcoFlow Developer API).
- Reads a configurable "grid present" field (`GRID_FIELD`) — grid is considered
  present while its value is above `GRID_THRESHOLD`.
- Debounces state changes (`STATE_CONFIRM_COUNT` consecutive readings) so a
  single flaky poll does not trigger a false alarm.
- Persists state to `monitor_state.json`, so a restart mid-outage still reports
  the correct total duration on recovery.
- Alerts once if the API is unreachable for `API_FAIL_ALERT_COUNT` polls.

## Setup

### 1. EcoFlow Developer API keys

You do not have these yet. Get them:

1. Go to <https://developer-eu.ecoflow.com> and sign in with your EcoFlow account.
2. Apply for developer access (approval is usually quick).
3. Create an application → you receive an **AccessKey** and **SecretKey**.
4. Find your device **serial number (SN)** in the EcoFlow app (device settings)
   or via the API device list.

> EU accounts use `https://api-e.ecoflow.com`. Other regions use
> `https://api.ecoflow.com` — set `ECOFLOW_BASE_URL` accordingly.

### 2. Telegram

You already have the bot token and chat_id. Put them in `.env`.

### 3. Configure

```bash
cp .env.example .env
# edit .env with your keys, SN, token, chat_id
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. Find the right grid + temperature fields

River 3 Plus field names are not confirmed. Dump every quota field:

```bash
.venv/bin/python ecoflow_monitor.py --discover
```

From the output, pick:

- the **grid/AC-input** field (look for keys like `acInVol`, `inputWatts`,
  `acInputWatts`) → set `GRID_FIELD` and a sensible `GRID_THRESHOLD`.
- a **temperature** field (e.g. a `temp` key) → set `TEMP_FIELD`. If the value
  looks like deci-degrees (e.g. `253` for 25.3°C) set `TEMP_DIVISOR=10`.

Verify a single reading:

```bash
.venv/bin/python ecoflow_monitor.py --once
```

### 5. Run

```bash
.venv/bin/python ecoflow_monitor.py
```

## Deploy on the VPS (systemd)

Deploy path: commit + push to git, then pull on the VPS.

```bash
sudo cp ecoflow-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ecoflow-monitor
sudo systemctl status ecoflow-monitor
journalctl -u ecoflow-monitor -f
```

Adjust `User`, `WorkingDirectory`, and paths in the unit file to match the VPS.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `ECOFLOW_BASE_URL` | `https://api-e.ecoflow.com` | API base (EU) |
| `ECOFLOW_ACCESS_KEY` | — | Developer API access key |
| `ECOFLOW_SECRET_KEY` | — | Developer API secret key |
| `ECOFLOW_DEVICE_SN` | — | Device serial number |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat id |
| `DEVICE_NAME` | `The Farm Internet` | Name shown in messages |
| `POLL_INTERVAL_SECONDS` | `30` | Poll interval |
| `GRID_FIELD` | `inv.acInVol` | Quota key for grid presence |
| `GRID_THRESHOLD` | `1000` | Grid present when value > threshold |
| `TEMP_FIELD` | `bms_bmsStatus.temp` | Quota key for temperature |
| `TEMP_DIVISOR` | `1` | Divide temp value (10 for deci-degrees) |
| `STATE_CONFIRM_COUNT` | `2` | Readings needed to confirm a change |
| `API_FAIL_ALERT_COUNT` | `10` | Failed polls before an API-down alert |

## Run modes

| Command | Effect |
|---|---|
| `python ecoflow_monitor.py` | Start the monitoring loop |
| `python ecoflow_monitor.py --discover` | Dump all quota fields, then exit |
| `python ecoflow_monitor.py --once` | Single poll, print state, no Telegram |
