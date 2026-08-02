# OUR FARM - EcoFlow bridge on the Raspberry Pi

The bridge logs in to EcoFlow, keeps a persistent MQTT connection, decodes the
River 3 protobuf, and serves the latest status as JSON at
`http://192.168.0.10:8080/status`. The ESP32 display just reads that JSON.

You do **not** need to open the EcoFlow app for this to work - the River pushes
data on its own as long as it is online.

## 1. Copy the files to the Pi

Put these three files in `/home/pi/ecoflow-bridge/` on the Pi:

- `ecoflow_bridge.py`
- `ef_river3.proto`
- `river-bridge.service`

(Use Finder "Connect to Server" `smb://192.168.0.10`, or `scp` from the Mac:
`scp ecoflow_bridge.py ef_river3.proto river-bridge.service pi@192.168.0.10:~/ecoflow-bridge/`)

## 2. Install dependencies (on the Pi, via SSH)

```
ssh pi@192.168.0.10
sudo apt update && sudo apt install -y python3-pip
pip3 install paho-mqtt protobuf grpcio-tools --break-system-packages
```

## 3. Fill in your details

Edit `ecoflow_bridge.py` and set `PASSWORD` (email and SN are already filled in).

## 4. Test it once

```
cd ~/ecoflow-bridge
python3 ecoflow_bridge.py
```

From the Mac, open `http://192.168.0.10:8080/status` in a browser - you should
see JSON like:

```json
{"soc": 92, "in_w": 6, "out_w": 6, "source": "mains",
 "mode": "standby", "dsg_rem_min": 2059, "online": true, ...}
```

Stop it with Ctrl-C once you've confirmed it works.

## 5. Run it automatically at boot

```
sudo cp ~/ecoflow-bridge/river-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now river-bridge
sudo systemctl status river-bridge          # check it's "active (running)"
journalctl -u river-bridge -f               # live logs
```

If your Pi user isn't `pi`, edit `User=` and the paths in the .service file.

## Troubleshooting

- **`online: false` in the JSON** -> no data received for 3 min. Usually the
  River went offline (lost its own Wi-Fi) or login failed. Check the logs.
- **Login fails** -> wrong password, or the account was made with Google/Apple
  sign-in (set an email password in the app first).
- **Display shows "Bridge offline"** -> the Pi is reachable but the River isn't
  sending. Confirm the River has internet.
- **Display shows "HTTP -1" / connection error** -> the ESP can't reach the Pi.
  Check the Pi's IP is still 192.168.0.10 (give it a static/reserved IP in your
  router) and that `river-bridge` is running.
