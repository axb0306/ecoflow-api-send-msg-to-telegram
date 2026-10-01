#!/usr/bin/env python3
"""EcoFlow River 3 -> JSON bridge + Telegram power alerts.

Logs in with an EcoFlow app account, keeps a standing MQTT connection,
decodes River 3 protobuf, and serves the latest status as JSON on
    http://<pi-ip>:8080/status

An ESP32 (or anything else on the LAN) fetches that JSON. The app does not
need to be open -- the River pushes data on its own while it is online.

Telegram alerts:
  * grid lost / grid restored (source flips between "mains" and "battery")
  * data stall: the MQTT session goes quiet while the socket stays open.
    Without this, a silent stall looks identical to "all is well" and no
    outage alert can ever fire.

Configuration comes from environment variables (see .env.example).

Install on the Pi:
    sudo apt install python3-pip -y
    pip3 install paho-mqtt protobuf python-dotenv --break-system-packages
Keep ef_river3_pb2.py next to this file. For autostart see river-bridge.service.
"""
import base64
import json
import os
import random
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(HERE, ".env"))
except ImportError:  # dotenv is optional; systemd may supply the env instead
    pass


def _env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


# --- Configuration ---
EMAIL = _env("ECOFLOW_EMAIL", required=True)
PASSWORD = _env("ECOFLOW_PASSWORD", required=True)
SN = _env("ECOFLOW_DEVICE_SN", required=True)
DOMAIN = _env("ECOFLOW_APP_DOMAIN", "api.ecoflow.com")
HTTP_HOST = _env("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(_env("HTTP_PORT", "8080"))

# TELEGRAM_ENABLED=0 -> status JSON only, no alerts (e.g. the Pi that feeds the
# ESP32 while another host owns the alerts).
TELEGRAM_ENABLED = _env("TELEGRAM_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
TELEGRAM_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=TELEGRAM_ENABLED)
TELEGRAM_CHAT = _env("TELEGRAM_CHAT_ID", required=TELEGRAM_ENABLED)
DEVICE_NAME = _env("DEVICE_NAME", "OUR FARM")

# A source change must hold this long before it is treated as real (anti-flicker).
CONFIRM_SEC = int(_env("CONFIRM_SEC", "15"))

# No decoded message for this long -> tear the MQTT session down and log in again.
# The socket can stay ESTABLISHED while delivery is already dead, so a
# connection-level check is not enough; this watches actual messages.
STALL_RECONNECT_SEC = int(_env("STALL_RECONNECT_SEC", "300"))

# No decoded message for this long -> Telegram alert. Deliberately longer than
# STALL_RECONNECT_SEC so a stall that one reconnect fixes stays quiet.
STALL_ALERT_SEC = int(_env("STALL_ALERT_SEC", "900"))

try:
    sys.stdout.reconfigure(line_buffering=True)  # so the log shows up live in journalctl
except Exception:
    pass
sys.path.insert(0, HERE)
try:
    import ef_river3_pb2 as pb  # pre-compiled; no grpcio-tools needed
except ImportError:
    sys.exit("Missing ef_river3_pb2.py next to this script.")
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

# --- Shared status (guarded by lock) ---
lock = threading.Lock()
state = {
    "soc": None, "in_w": 0, "out_w": 0,
    "source": "unknown",      # "mains" / "battery"
    "mode": "unknown",        # "charging" / "discharging" / "standby"
    "dsg_rem_min": None, "chg_rem_min": None,
    "batt_temp": None, "batt_volt": None,
    "updated": 0, "online": False,
}

# Set by the watchdog to make the MQTT loop drop its session and log in again.
reconnect_now = threading.Event()


def http_post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def http_get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def login():
    """Return (token, user_id, mqtt certificate)."""
    r = http_post(f"https://{DOMAIN}/auth/login",
                  {"lang": "en_US", "content-type": "application/json"},
                  {"email": EMAIL,
                   "password": base64.b64encode(PASSWORD.encode()).decode(),
                   "scene": "IOT_APP", "userType": "ECOFLOW"})
    if str(r.get("message", "")).lower() != "success":
        raise RuntimeError(f"Login failed: {r}")
    token = r["data"]["token"]
    user_id = r["data"]["user"]["userId"]
    cert = http_get(f"https://{DOMAIN}/iot-auth/app/certification",
                    {"lang": "en_US", "authorization": f"Bearer {token}"})["data"]
    return token, user_id, cert


# --- Protobuf decoding ---
def decode(raw):
    hm = pb.River3HeaderMessage()
    try:
        hm.ParseFromString(raw)
    except Exception:
        return {}
    out = {}
    for h in hm.header:
        pd = h.pdata
        if not pd:
            continue
        if h.enc_type == 1 and h.src != 32:
            pd = bytes((b ^ (h.seq & 0xFF)) & 0xFF for b in pd)
        if h.cmd_func == 254 and h.cmd_id == 21:
            m = pb.River3DisplayPropertyUpload()
        elif h.cmd_func == 254 and h.cmd_id == 22:
            m = pb.River3RuntimePropertyUpload()
        elif h.cmd_func == 32 and h.cmd_id == 2:
            m = pb.River3CMSHeartBeatReport()
        else:
            continue
        try:
            m.ParseFromString(pd)
        except Exception:
            continue
        if h.cmd_func == 32:
            for f, v in m.msg32_2_1.ListFields():
                out[f.name] = v
        else:
            for f, v in m.ListFields():
                out[f.name] = v
    return out


def apply_fields(d):
    with lock:
        if "cms_batt_soc" in d: state["soc"] = round(d["cms_batt_soc"])
        elif "bms_batt_soc" in d: state["soc"] = round(d["bms_batt_soc"])
        if "pow_in_sum_w" in d:  state["in_w"] = round(d["pow_in_sum_w"])
        if "pow_out_sum_w" in d: state["out_w"] = round(d["pow_out_sum_w"])
        # Above ~50h is EcoFlow's "no time applies" value (e.g. 5939) -> None
        NO_TIME = 3000
        if "cms_dsg_rem_time" in d:
            v = d["cms_dsg_rem_time"]; state["dsg_rem_min"] = None if v >= NO_TIME else v
        if "cms_chg_rem_time" in d:
            v = d["cms_chg_rem_time"]; state["chg_rem_min"] = None if v >= NO_TIME else v
        if "bms_max_cell_temp" in d: state["batt_temp"] = d["bms_max_cell_temp"]
        if "bms_batt_vol" in d: state["batt_volt"] = round(d["bms_batt_vol"], 2)

        st = d.get("cms_chg_dsg_state", d.get("bms_chg_dsg_state"))
        ac_in = d.get("plug_in_info_ac_in_flag")
        if ac_in is not None or "pow_in_sum_w" in d:
            on_mains = bool(ac_in) or state["in_w"] > 0
            state["source"] = "mains" if on_mains else "battery"
        if st is not None:
            state["mode"] = {0: "standby", 1: "discharging", 2: "charging"}.get(st, "standby")
        state["updated"] = int(time.time())
        state["online"] = True
    notify_data_resumed()   # outside the lock - may send Telegram
    check_power_alerts()


# --- Telegram ---
def send_telegram(text):
    if not TELEGRAM_ENABLED:
        return

    def _do():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": text}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
        except Exception as e:
            print("Telegram error:", e)
    threading.Thread(target=_do, daemon=True).start()


def fmt_duration(sec):
    sec = int(sec); h = sec // 3600; m = (sec % 3600) // 60
    if h: return f"{h} h {m} min"
    if m: return f"{m} min"
    return f"{sec} sec"


# stable = last confirmed source; cand = candidate during debounce
_alert = {"stable": None, "cand": None, "cand_since": 0.0, "outage_start": None}

# Tracks whether a "no data" alert is currently outstanding.
_stall = {"notified": False, "since": 0.0}


def check_power_alerts():
    now = time.time()
    with lock:
        src  = state["source"]
        soc  = state["soc"]
        temp = state["batt_temp"]
    if src not in ("mains", "battery"):
        return
    # Debounce: the source must hold steady for CONFIRM_SEC before we act
    if src != _alert["cand"]:
        _alert["cand"] = src
        _alert["cand_since"] = now
        return
    if now - _alert["cand_since"] < CONFIRM_SEC:
        return
    if _alert["stable"] is None:      # first stable reading = baseline, no alert
        _alert["stable"] = src
        if src == "battery":          # started in the middle of an outage
            _alert["outage_start"] = _alert["cand_since"]
        return
    if src == _alert["stable"]:
        return
    _alert["stable"] = src
    if src == "battery":
        _alert["outage_start"] = _alert["cand_since"]
        msg = (f"{DEVICE_NAME}: POWER OUTAGE\n"
               f"River switched to battery.\n"
               f"Battery: {soc}%")
        send_telegram(msg)
        print("Telegram: power outage")
    else:  # back on mains
        start = _alert["outage_start"]
        _alert["outage_start"] = None
        dur = fmt_duration(_alert["cand_since"] - start) if start else "unknown"
        msg = (f"{DEVICE_NAME}: MAINS POWER RESTORED\n"
               f"Time on battery: {dur}\n"
               f"Battery: {soc}%\n"
               f"Battery temp: {temp} C")
        send_telegram(msg)
        print("Telegram: mains restored")


def notify_data_resumed():
    """Clear an outstanding stall alert once real data arrives again."""
    if not _stall["notified"]:
        return
    gap = fmt_duration(time.time() - _stall["since"]) if _stall["since"] else "unknown"
    _stall["notified"] = False
    _stall["since"] = 0.0
    with lock:
        src = state["source"]
        soc = state["soc"]
    send_telegram(f"{DEVICE_NAME}: DATA BACK\n"
                  f"River is reporting again after {gap}.\n"
                  f"Source: {src}\nBattery: {soc}%")
    print("Telegram: data resumed")


POLL_SEC    = 50    # ask for "latest status" this often
OFFLINE_SEC = 240   # mark offline only after this much silence

_seen_reply = {"done": False}


def handle_get_reply(payload):
    """The reply may be JSON (quotaMap) OR protobuf. Try both."""
    # 1) JSON variant
    try:
        data = json.loads(payload)
        if data.get("operateType") == "latestQuotas":
            qm = (data.get("data") or {}).get("quotaMap") or {}
            if qm:
                if not _seen_reply["done"]:
                    _seen_reply["done"] = True
                    print("get_reply (JSON) quotaMap keys:", list(qm.keys())[:40])
                apply_fields(qm)
                return
    except Exception:
        pass
    # 2) protobuf variant (same decoding as the data stream)
    d = decode(payload)
    if d:
        if not _seen_reply["done"]:
            _seen_reply["done"] = True
            print("get_reply (protobuf) fields:", list(d.keys())[:40])
        apply_fields(d)


def watchdog_loop():
    """Watch message flow, not just the socket.

    An EcoFlow MQTT session can go silent while the TCP connection stays
    ESTABLISHED and paho still reports rc = 0. Reconnecting on a timer alone
    does not catch that, so this thread measures time since the last decoded
    message and (a) forces a fresh login, then (b) raises a Telegram alert if
    the silence outlives the reconnect.
    """
    baseline = time.time()   # start counting from process start, not epoch 0
    last_forced = 0.0
    while True:
        time.sleep(10)
        with lock:
            updated = state["updated"]
        last_data = updated if updated else baseline
        silent = time.time() - last_data

        if silent >= STALL_RECONNECT_SEC and time.time() - last_forced >= STALL_RECONNECT_SEC:
            last_forced = time.time()
            print(f"No data for {int(silent)}s - forcing MQTT reconnect")
            reconnect_now.set()

        if silent >= STALL_ALERT_SEC and not _stall["notified"]:
            _stall["notified"] = True
            _stall["since"] = last_data
            with lock:
                src = state["source"]
                soc = state["soc"]
            last_seen = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_data))
                         if updated else "never since start")
            send_telegram(f"{DEVICE_NAME}: NO DATA FROM RIVER\n"
                          f"Silent for {fmt_duration(silent)} (last seen {last_seen}).\n"
                          f"Last known source: {src}, battery {soc}%.\n"
                          f"Power state is UNKNOWN - check the farm.")
            print("Telegram: no data alert")


# --- MQTT loop with auto-reconnect + re-login + active polling ---
def mqtt_loop():
    while True:
        cl = None
        try:
            _token, user_id, cert = login()
            print("Logged in, connecting MQTT", cert["url"])
            data_topic  = f"/app/device/property/{SN}"
            get_topic   = f"/app/{user_id}/{SN}/thing/property/get"
            reply_topic = f"/app/{user_id}/{SN}/thing/property/get_reply"

            def on_connect(cl, u, f, rc, props=None, *, data_topic=data_topic, reply_topic=reply_topic):
                print("MQTT connected rc =", rc)
                cl.subscribe(data_topic)
                cl.subscribe(reply_topic)

            def on_message(cl, u, msg, *, reply_topic=reply_topic):
                if msg.topic == reply_topic:
                    handle_get_reply(msg.payload)
                    return
                d = decode(msg.payload)
                if d:
                    apply_fields(d)

            cl = mqtt.Client(
                client_id=f"ANDROID_{random.getrandbits(64):016X}_{user_id}",
                protocol=mqtt.MQTTv311)
            cl.username_pw_set(cert["certificateAccount"], cert["certificatePassword"])
            cl.tls_set(cert_reqs=ssl.CERT_NONE)
            cl.on_connect = on_connect
            cl.on_message = on_message
            cl.connect(cert["url"], int(cert["port"]))
            cl.loop_start()

            def poll(client=cl, topic=get_topic):
                """Ask for the latest status - keeps the flow alive without the app."""
                req = json.dumps({"version": "1.1", "moduleType": 0,
                                  "operateType": "latestQuotas", "params": {}})
                client.publish(topic, req)

            reconnect_now.clear()
            time.sleep(2)
            poll()   # ask for data right away

            # Run for 6 hours, then log in again (the token expires)
            t0 = time.time()
            last_poll = time.time()
            while time.time() - t0 < 6 * 3600 and not reconnect_now.is_set():
                time.sleep(5)
                if time.time() - last_poll >= POLL_SEC:
                    last_poll = time.time()
                    try:
                        poll()
                    except Exception:
                        pass
                with lock:
                    if state["updated"] and time.time() - state["updated"] > OFFLINE_SEC:
                        state["online"] = False
            if reconnect_now.is_set():
                print("Watchdog asked for a reconnect - restarting MQTT session")
                reconnect_now.clear()
        except Exception as e:
            print("MQTT error, retrying in 30 s:", e)
            with lock:
                state["online"] = False
            time.sleep(30)
        finally:
            if cl is not None:
                try:
                    cl.loop_stop()
                    cl.disconnect()
                except Exception:
                    pass


# --- Small HTTP server ---
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") in ("/status", ""):
            with lock:
                out = dict(state)
                # age_sec = exact age of the data (since the last real update)
                out["age_sec"] = (int(time.time()) - state["updated"]
                                  if state["updated"] else None)
                body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass  # quiet


def main():
    send_telegram(f"{DEVICE_NAME}: monitoring started. "
                  "You'll be alerted on power outage or loss of contact.")
    threading.Thread(target=mqtt_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    print(f"JSON status on http://{HTTP_HOST}:{HTTP_PORT}/status "
          f"(Telegram {'on' if TELEGRAM_ENABLED else 'off'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
