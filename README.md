# Nakai 2026 — ROV Controller

A web-based control system for ROV-1. Runs on a Jetson Nano via Docker and serves a browser UI for controlling movement, impeller, and brush systems, with a live video feed from a Basler USB3 camera.

---

## System Overview

```
Browser  ──WebSocket──▶  Flask/SocketIO Server (Jetson Nano)
                              │
                    ┌─────────┴──────────┐
                    │                    │
               Teensy (USB)        Basler Camera (USB3)
               motors / sensors    live video stream
```

**Components:**
- **`server.py`** — Flask + SocketIO backend. Manages serial connection to the Teensy, camera stream, and relays commands/telemetry to the browser.
- **`templates/index.html`** — Cyberpunk HUD interface. Works on desktop (keyboard) and mobile (touch).
- **`Dockerfile` / `docker-compose.yml`** — Containerized deployment for the Jetson Nano.
- **`teensey_code`** — Firmware running on the Teensy microcontroller.

---

## Hardware Requirements

| Component | Details |
|---|---|
| Jetson Nano | JetPack 4.6+ (Ubuntu 18.04) or JetPack 5.x (Ubuntu 20.04) |
| Basler USB3 camera | Detected automatically via pypylon |
| Teensy (PJRC) | Connected via USB, auto-detected on `/dev/ttyACM*` |
| Network | Ethernet or Wi-Fi on the same network as the operator |

---

## Web UI

Open a browser and navigate to `http://<jetson-ip>:8080`

### HUD Layout

```
┌─────────────────────────────────────────────────────┐
│ ROV·1          ▲ FORWARD                    ● SNAP  │
│                                                     │
│  RANGE                                  IMPELLER    │
│  ---  mm                                   0%       │
│                                         PWM · 1500  │
│  SPEED                                              │
│  5 / 10                              SYSTEMS        │
│  ───────                             BRUSH  OFF     │
│                                      H2O    OK      │
│                                      TEENSY ---     │
│                                                     │
│   [▲]          SPD IMP               [SPD+][IMP+]   │
│  [◀][■][▶]     ─── ───               [SPD-][IMP-]   │
│   [▼]          PWR                   [IMP0][BRUSH]  │
└─────────────────────────────────────────────────────┘
```

### Keyboard Controls

| Key | Action |
|---|---|
| `W` / `S` / `A` / `D` | Forward / Reverse / Left / Right (hold to move) |
| `X` | Emergency stop |
| `+` / `-` | Speed up / down (1–10) |
| `↑` / `↓` | Impeller power up / down (steps of 10%) |
| `0` | Impeller stop |
| `B` | Toggle brush |

Touch controls mirror the on-screen buttons.

### Telemetry (live, updates at ~10 Hz)

| Field | Source |
|---|---|
| **Range** | Distance sensor via Teensy serial (`Distance: <mm>`) |
| **Speed** | Current speed setting (1–10) |
| **Impeller** | Power % and PWM value (1500 µs center) |
| **Brush** | ON/OFF state echoed from Teensy |
| **H2O** | Water ingress warning from Teensy (`Water` in serial line) |
| **Teensy** | Connection status |

---

## Code Walkthrough

### `server.py` — How the server works

The server has three background threads that run independently from startup:

```python
threading.Thread(target=connect_serial, daemon=True).start()   # keeps Teensy connected
threading.Thread(target=read_telemetry, daemon=True).start()   # reads sensor data
threading.Thread(target=camera_thread, daemon=True).start()    # grabs camera frames
```

**Teensy auto-detection**

Instead of hardcoding a port like `/dev/ttyACM0`, the server scans all connected USB serial devices and matches by Teensy's USB Vendor ID (`0x16C0`). Inside Docker where VID metadata is unavailable, it falls back to any `ttyACM` device:

```python
TEENSY_VID = 0x16C0

def find_teensy():
    for port in serial.tools.list_ports.comports():
        if port.vid == TEENSY_VID:
            return port.device          # exact VID match (bare metal)
    for port in serial.tools.list_ports.comports():
        if 'ttyACM' in port.device:
            return port.device          # fallback for Docker
    return None
```

If the Teensy disconnects mid-session, `connect_serial` detects the dead port and keeps scanning until it reconnects — no restart needed.

**Telemetry parsing**

The `read_telemetry` thread reads lines from the Teensy and updates the shared `state` dict, then broadcasts it to all connected browsers over SocketIO:

```python
if line.startswith("Distance:"):
    state['distance_mm'] = int(line.split(':')[1].strip())
elif line.startswith("Brush Motor:"):
    state['brush_on'] = "ON" in line
elif "Water" in line:
    state['water_warning'] = True      # auto-clears after 2 seconds
```

**Camera stream**

Frames from the Basler camera are grabbed in a loop, resized to 960×540, JPEG-encoded, and stored in a shared `latest_frame` variable protected by a lock:

```python
frame = cv2.resize(frame, (960, 540))
_, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
with frame_lock:
    latest_frame = jpeg.tobytes()
```

The `/video_feed` endpoint reads from `latest_frame` and streams it as MJPEG (multipart HTTP). If no camera is connected, it serves a grey "No Camera" placeholder instead of erroring.

**Handling a command from the browser**

When the operator presses a key or button, the browser sends a SocketIO `command` event. The server updates its internal `state`, writes the ASCII command to the Teensy over serial, then broadcasts the updated state back to all clients:

```python
@socketio.on('command')
def on_command(data):
    cmd = data.get('cmd')
    if cmd in ('w', 'a', 's', 'd'):
        state['direction'] = direction_map[cmd]
        send_serial(cmd)               # e.g. writes "w\n" to /dev/ttyACM0
    ...
    socketio.emit('state', state)      # update every connected browser
```

---

### `templates/index.html` — How the UI works

The UI is a single HTML file. The camera feed is a full-screen `<img>` tag pointing at `/video_feed` — the browser continuously receives MJPEG frames and updates the image automatically. All the controls and readouts are absolutely positioned on top using CSS.

**Sending a command**

Every button calls `sendCmd()` which fires a SocketIO event:

```js
function sendCmd(cmd) {
    socket.emit('command', { cmd });
}
```

**Direction hold (WASD)**

Holding a direction key should keep the ROV moving, not just nudge it. The UI tracks which keys are currently held in a `Set` and resolves the highest-priority direction each time a key is pressed or released:

```js
const heldDirs = new Set();
const dirPriority = ['w', 's', 'a', 'd'];

function pressDir(k)   { heldDirs.add(k);    resolveDir(); }
function releaseDir(k) { heldDirs.delete(k); resolveDir(); }

function resolveDir() {
    for (const k of dirPriority) {
        if (heldDirs.has(k)) { sendCmd(k); return; }
    }
    sendCmd('x');   // nothing held → stop
}
```

This also means holding `W` then pressing `A` mid-move switches direction instantly, and releasing `A` resumes `W` — matching what you'd expect from a game controller.

**Receiving telemetry**

The browser listens for `state` events from the server and updates every HUD element:

```js
socket.on('state', s => {
    // Direction badge
    document.getElementById('dir-badge').textContent = s.direction;

    // Speed bar fill
    const pct = (s.speed / s.speed_max * 100) + '%';
    document.getElementById('spd-bar').style.width = pct;

    // Water warning — blinks a red overlay
    if (s.water_warning) {
        document.getElementById('water-overlay').classList.add('show');
    } else {
        document.getElementById('water-overlay').classList.remove('show');
    }
    // ... and so on for impeller, brush, range, Teensy status
});
```

**Impeller split bar**

The impeller can run forward or reverse (−100% to +100%). The bar grows from center — right for positive, left for negative — with different colors:

```js
function setImpFill(id, pwr) {
    const el = document.getElementById(id);
    if (pwr > 0) {
        el.style.left  = '50%';
        el.style.width = (pwr / 2) + '%';
        el.style.background = 'var(--ok)';      // green for forward
    } else if (pwr < 0) {
        const w = Math.abs(pwr) / 2;
        el.style.left  = (50 - w) + '%';
        el.style.width = w + '%';
        el.style.background = 'var(--cyan)';    // cyan for reverse
    } else {
        el.style.width = '0%';
    }
}
```

---

## Jetson Nano Setup

> See **[JETSON_SETUP.md](JETSON_SETUP.md)** for the full step-by-step guide.

**Quick start:**

```bash
# 1. Clone the repo onto the Jetson
git clone <repo-url> ~/nakai-2026
cd ~/nakai-2026

# 2. Build and run
docker compose up --build

# 3. Open in browser
# http://<jetson-ip>:8080
```

The `docker-compose.yml` runs the container in `privileged` mode so all USB devices (Teensy serial, Basler camera) are accessible without extra configuration.

---

## Local Development (without Docker)

```bash
# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the server
python server.py
```

The server starts on `http://localhost:8080`.

> **Note:** `pypylon` requires the [Pylon Camera Software Suite](https://www.baslerweb.com/en/downloads/software-downloads/) to be installed on your machine. If it's not installed, the server starts normally but skips the camera and shows a "No Camera" placeholder.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BAUD_RATE` | `115200` | Serial baud rate for Teensy communication |

Example:
```bash
BAUD_RATE=9600 python server.py
```

Or in `docker-compose.yml`:
```yaml
services:
  controller:
    environment:
      - BAUD_RATE=9600
```

---

## Serial Protocol (Jetson → Teensy)

Commands sent as ASCII strings terminated with `\n`:

| Command | Meaning |
|---|---|
| `w` | Move forward |
| `s` | Move reverse |
| `a` | Turn left |
| `d` | Turn right |
| `x` | Stop movement |
| `+` | Increase speed |
| `-` | Decrease speed |
| `<number>` | Set impeller power (e.g. `50`, `-30`) |
| `0` | Impeller stop |
| `brush_on` | Turn brush on |
| `brush_off` | Turn brush off |

Telemetry received from the Teensy (ASCII lines):

| Format | Description |
|---|---|
| `Distance: <mm>` | Range sensor reading in millimetres |
| `Brush Motor: ON/OFF` | Brush state confirmation |
| `Water ...` | Triggers water ingress warning |

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Main HUD interface |
| `GET /video_feed` | MJPEG stream from the Basler camera |
| `GET /snapshot` | Single JPEG frame |
| `WS /socket.io` | SocketIO for commands and telemetry |

---

## Project Structure

```
.
├── server.py             # Main Flask/SocketIO application
├── templates/
│   └── index.html        # Browser HUD
├── Dockerfile            # Container build
├── docker-compose.yml    # Deployment config
├── requirements.txt      # Python dependencies
├── setup_service.py      # Systemd service installer (run on Jetson)
├── teensey_code          # Teensy firmware source
├── JETSON_SETUP.md       # Full Jetson Nano setup guide
└── POC/                  # Proof-of-concept scripts and experiments
```

---

## Auto-Start on Boot

Use `setup_service.py` on the Jetson to install a systemd service that starts the container automatically on boot:

```bash
sudo python3 setup_service.py
```

Or follow the manual instructions in [JETSON_SETUP.md](JETSON_SETUP.md).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Camera shows "No Camera" | Check `lsusb \| grep Basler` on the Jetson; ensure camera is plugged in before container starts |
| Teensy not found | Check `ls /dev/ttyACM*`; server auto-scans and reconnects when it appears |
| Can't reach `:8080` from browser | Run `sudo ufw allow 8080` on the Jetson |
| Docker build fails / runs out of memory | Add swap: `sudo fallocate -l 4G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` |
| `opencv` install fails on ARM64 | Add `libgl1-mesa-glx libglib2.0-0` to the `apt-get` line in `Dockerfile` |
