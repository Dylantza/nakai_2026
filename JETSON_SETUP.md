# Jetson Nano Setup Guide

This guide covers deploying the Nakai controller server on a Jetson Nano via Docker.

The server provides:
- Web UI for controlling the robot (movement, impeller, brush)
- Live video stream from a Basler USB3 camera
- Real-time telemetry from a Teensy microcontroller over serial

---

## Requirements

- Jetson Nano with JetPack 4.6+ or 5.x
- Basler USB3 camera
- Teensy connected via USB
- Network connection (ethernet or Wi-Fi)

---

## Step 1 — Check JetPack Version

```bash
cat /etc/nv_tegra_release
```

JetPack 4.6 = Ubuntu 18.04, JetPack 5.x = Ubuntu 20.04. Either works.

---

## Step 2 — Install Docker & Docker Compose

Docker comes pre-installed with JetPack. Enable it and add your user to the docker group:

```bash
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
```

Log out and back in after the `usermod` command.

Check if Docker Compose is available:

```bash
docker compose version
```

If not found, install it:

```bash
sudo apt-get install -y docker-compose-plugin
```

> **JetPack 4.x fallback:** If `docker compose` (v2) is unavailable, install v1 and replace `docker compose` with `docker-compose` in all commands below.
> ```bash
> sudo apt-get install -y docker-compose
> ```

---

## Step 3 — Get the Project onto the Jetson

**Option A — Clone from Git (recommended):**

```bash
git clone <repo-url> ~/nakai-2026
```

**Option B — Copy from another machine:**

Run this from inside the project folder on your computer:

```bash
cd /path/to/nakai-2026

rsync -av \
  --exclude venv \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  . \
  <user>@<jetson-ip>:~/nakai-2026/
```

---

## Step 4 — Build and Run

```bash
cd ~/nakai-2026
docker compose up --build
```

The first build takes **10–20 minutes** while it downloads and compiles ARM64 packages. Subsequent starts are fast.

Once running, open a browser on any device on the same network:

```
http://<jetson-ip>:8080
```

---

## Step 5 — Verify Devices

The server needs the Teensy (`/dev/ttyACM*`) and Basler camera. The `docker-compose.yml` uses `privileged: true` so USB devices are passed through automatically.

Confirm the Jetson sees them before starting:

```bash
# Teensy serial port
ls /dev/ttyACM*

# Basler camera
lsusb | grep Basler
```

The server will auto-scan for the Teensy on startup and reconnect if it disconnects.

---

## Step 6 — Auto-Start on Boot

Create a systemd service so the server starts automatically on reboot.

```bash
sudo nano /etc/systemd/system/nakai.service
```

Paste the following (replace `nano` with your username if different):

```ini
[Unit]
Description=Nakai 2026 Controller
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/home/nano/nakai-2026
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=10
User=nano

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable nakai
sudo systemctl start nakai
```

Check status:

```bash
sudo systemctl status nakai
```

---

## Common Commands

```bash
# Start in background
docker compose up -d

# View live logs
docker compose logs -f

# Stop
docker compose down

# Rebuild after code changes
docker compose up --build -d
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `opencv-python-headless` fails to install | Add `libgl1-mesa-glx libglib2.0-0` to the `apt-get` line in `Dockerfile` |
| Camera not detected inside Docker | Verify `privileged: true` is in `docker-compose.yml`; check `lsusb` on host |
| Teensy not found | Run `ls /dev/ttyACM*` on host to confirm it's connected; server will auto-scan on reconnect |
| Port 8080 unreachable from other devices | Run `sudo ufw allow 8080` on the Jetson |
| Build fails or runs out of memory | Add swap space: |

```bash
# Add 4GB swap (run once, persists across reboots if added to /etc/fstab)
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```
