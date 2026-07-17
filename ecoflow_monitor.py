#!/usr/bin/env python3
"""EcoFlow power monitor -> Telegram notifier.

Polls the EcoFlow Developer API for a single device on a fixed interval.
When grid/AC input power disappears, sends a Telegram alert. When power
returns, sends a recovery message including the total outage duration and
the current temperature.

Device: "The Farm Internet" (EcoFlow River 3 Plus Wireless).

Run modes:
    python ecoflow_monitor.py            # start the monitoring loop
    python ecoflow_monitor.py --discover # dump every quota field once, then exit
    python ecoflow_monitor.py --once     # single poll, print state, no Telegram

Configuration is read from environment variables (see .env.example).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional; env vars may be provided by systemd
    pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        logging.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


ECOFLOW_BASE_URL = _env("ECOFLOW_BASE_URL", "https://api-e.ecoflow.com")
ECOFLOW_ACCESS_KEY = _env("ECOFLOW_ACCESS_KEY", required=True)
ECOFLOW_SECRET_KEY = _env("ECOFLOW_SECRET_KEY", required=True)
ECOFLOW_DEVICE_SN = _env("ECOFLOW_DEVICE_SN", required=True)

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", required=True)

DEVICE_NAME = _env("DEVICE_NAME", "The Farm Internet")
POLL_INTERVAL = int(_env("POLL_INTERVAL_SECONDS", "30"))

# Quota field that indicates grid/AC input presence, and the threshold above
# which grid power is considered present. River 3 defaults below; override in
# .env once --discover reveals the exact keys for your unit.
GRID_FIELD = _env("GRID_FIELD", "inv.acInVol")
GRID_THRESHOLD = float(_env("GRID_THRESHOLD", "1000"))

# Quota field for temperature (deci-degrees on many EcoFlow models -> divide).
TEMP_FIELD = _env("TEMP_FIELD", "bms_bmsStatus.temp")
TEMP_DIVISOR = float(_env("TEMP_DIVISOR", "1"))

# Consecutive readings required before a state change is trusted (debounce).
STATE_CONFIRM_COUNT = int(_env("STATE_CONFIRM_COUNT", "2"))

# Alert if the API is unreachable for this many consecutive polls.
API_FAIL_ALERT_COUNT = int(_env("API_FAIL_ALERT_COUNT", "10"))

STATE_FILE = _env("STATE_FILE", os.path.join(os.path.dirname(__file__), "monitor_state.json"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ecoflow_monitor")


# --------------------------------------------------------------------------- #
# EcoFlow Developer API
# --------------------------------------------------------------------------- #

def _flatten(obj, prefix: str = "") -> dict:
    """Flatten nested dicts/lists into dotted keys, the way EcoFlow signs them."""
    items: dict[str, str] = {}
    if isinstance(obj, dict):
        for key in obj:
            new_key = f"{prefix}.{key}" if prefix else key
            items.update(_flatten(obj[key], new_key))
    elif isinstance(obj, list):
        for idx, val in enumerate(obj):
            items.update(_flatten(val, f"{prefix}[{idx}]"))
    else:
        items[prefix] = obj
    return items


def _sign(params: dict) -> dict:
    """Return the headers (including HMAC-SHA256 sign) for an EcoFlow request."""
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))

    sign_parts = []
    if params:
        flat = _flatten(params)
        for key in sorted(flat):
            sign_parts.append(f"{key}={flat[key]}")
    sign_parts.append(f"accessKey={ECOFLOW_ACCESS_KEY}")
    sign_parts.append(f"nonce={nonce}")
    sign_parts.append(f"timestamp={timestamp}")
    sign_str = "&".join(sign_parts)

    signature = hmac.new(
        ECOFLOW_SECRET_KEY.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "accessKey": ECOFLOW_ACCESS_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": signature,
        "Content-Type": "application/json",
    }


def get_all_quota() -> dict:
    """Fetch all quota fields for the configured device. Raises on API error."""
    query = {"sn": ECOFLOW_DEVICE_SN}
    headers = _sign(query)
    url = f"{ECOFLOW_BASE_URL}/iot-open/sign/device/quota/all"
    resp = requests.get(url, headers=headers, params=query, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("code")) != "0":
        raise RuntimeError(f"EcoFlow API error: {body.get('code')} {body.get('message')}")
    return body.get("data", {})


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        resp.raise_for_status()
        log.info("Telegram sent: %s", text.replace("\n", " | "))
    except Exception as exc:  # never let a Telegram failure kill the loop
        log.error("Telegram send failed: %s", exc)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def read_field(data: dict, field: str):
    """Read a dotted field from quota data, tolerant of flat or nested layout."""
    if field in data:
        return data[field]
    node = data
    for part in field.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def grid_present(data: dict) -> bool | None:
    value = read_field(data, GRID_FIELD)
    if value is None:
        return None
    try:
        return float(value) > GRID_THRESHOLD
    except (TypeError, ValueError):
        return None


def read_temperature(data: dict) -> float | None:
    value = read_field(data, TEMP_FIELD)
    if value is None:
        return None
    try:
        return float(value) / TEMP_DIVISOR
    except (TypeError, ValueError):
        return None


def fmt_duration(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    days, rem = divmod(int(td.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError as exc:
        log.error("Could not persist state: %s", exc)


# --------------------------------------------------------------------------- #
# Discovery mode
# --------------------------------------------------------------------------- #

def run_discover() -> None:
    data = get_all_quota()
    flat = _flatten(data) if not all(isinstance(v, (int, float, str, bool)) for v in data.values()) else data
    print(f"\n=== Quota fields for {DEVICE_NAME} ({ECOFLOW_DEVICE_SN}) ===\n")
    for key in sorted(flat):
        print(f"  {key} = {flat[key]}")
    print(
        "\nPick the grid/AC-input field (e.g. an acInVol or inputWatts key) for "
        "GRID_FIELD, and a temperature key for TEMP_FIELD, then set them in .env.\n"
    )


# --------------------------------------------------------------------------- #
# Monitor loop
# --------------------------------------------------------------------------- #

def poll_state() -> tuple[bool | None, float | None]:
    data = get_all_quota()
    return grid_present(data), read_temperature(data)


def run_once() -> None:
    present, temp = poll_state()
    print(f"grid_present={present} ({GRID_FIELD})  temperature={temp}°C ({TEMP_FIELD})")


def run_monitor() -> None:
    state = load_state()
    # Confirmed power state: True=on grid, False=outage, None=unknown at start.
    power_on = state.get("power_on")
    outage_start = state.get("outage_start")  # epoch seconds
    pending = None
    pending_count = 0
    api_fail_streak = 0

    log.info(
        "Monitor started. device=%s sn=%s interval=%ss grid_field=%s temp_field=%s",
        DEVICE_NAME, ECOFLOW_DEVICE_SN, POLL_INTERVAL, GRID_FIELD, TEMP_FIELD,
    )

    while True:
        try:
            present, temp = poll_state()
            api_fail_streak = 0

            if present is None:
                log.warning("Grid field '%s' not found in quota data; check GRID_FIELD.", GRID_FIELD)
            else:
                if power_on is None:
                    # First confirmed reading: adopt it silently, no alert.
                    power_on = present
                    log.info("Initial power state: %s", "ON" if present else "OUTAGE")
                    if not present and not outage_start:
                        outage_start = time.time()
                elif present != power_on:
                    # Debounce: require STATE_CONFIRM_COUNT matching readings.
                    if pending == present:
                        pending_count += 1
                    else:
                        pending = present
                        pending_count = 1

                    if pending_count >= STATE_CONFIRM_COUNT:
                        power_on = present
                        pending = None
                        pending_count = 0
                        if not present:
                            outage_start = time.time()
                            _notify_outage(temp)
                        else:
                            _notify_restore(outage_start, temp)
                            outage_start = None
                else:
                    pending = None
                    pending_count = 0

            save_state({"power_on": power_on, "outage_start": outage_start})

        except Exception as exc:
            api_fail_streak += 1
            log.error("Poll failed (%d): %s", api_fail_streak, exc)
            if api_fail_streak == API_FAIL_ALERT_COUNT:
                send_telegram(
                    f"⚠️ <b>{DEVICE_NAME}</b>\nMonitor cannot reach the EcoFlow API "
                    f"({api_fail_streak} failed polls). Power state unknown."
                )

        time.sleep(POLL_INTERVAL)


def _temp_line(temp: float | None) -> str:
    return f"🌡 Temperature: {temp:.1f}°C" if temp is not None else "🌡 Temperature: n/a"


def _notify_outage(temp: float | None) -> None:
    msg = (
        f"🔴 <b>{DEVICE_NAME}</b> — POWER LOST\n"
        f"Grid input gone at {now_local()}.\n"
        f"{_temp_line(temp)}"
    )
    log.warning("POWER LOST")
    send_telegram(msg)


def _notify_restore(outage_start: float | None, temp: float | None) -> None:
    if outage_start:
        duration = fmt_duration(time.time() - outage_start)
        started = datetime.fromtimestamp(outage_start).strftime("%Y-%m-%d %H:%M:%S")
        dur_line = f"Outage duration: <b>{duration}</b>\nStarted: {started}"
    else:
        dur_line = "Outage duration: unknown"
    msg = (
        f"🟢 <b>{DEVICE_NAME}</b> — POWER BACK\n"
        f"Grid input restored at {now_local()}.\n"
        f"{dur_line}\n"
        f"{_temp_line(temp)}"
    )
    log.info("POWER BACK")
    send_telegram(msg)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="EcoFlow power -> Telegram monitor")
    parser.add_argument("--discover", action="store_true", help="Dump all quota fields and exit")
    parser.add_argument("--once", action="store_true", help="Single poll, print state, no Telegram")
    args = parser.parse_args()

    if args.discover:
        run_discover()
    elif args.once:
        run_once()
    else:
        run_monitor()


if __name__ == "__main__":
    main()
