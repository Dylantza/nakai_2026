# Camera Refactor Plan: Basler USB3 → DWE exploreHD

## Overview

Replace the Basler USB3 camera (pypylon SDK) with the DeepWater Exploration exploreHD (standard UVC USB camera). The streaming architecture — MJPEG over HTTP — stays unchanged.

---

## Files to Change

| File | Change |
|------|--------|
| `server.py` | Remove pypylon import block, rewrite `camera_thread()` |
| `requirements.txt` | Remove `pypylon` |
| `Dockerfile` | Remove `libusb-1.0-0` apt install and its comment |

---

## 1. `server.py`

### Remove (lines 11–16)
```python
try:
    from pypylon import pylon
    PYLON_AVAILABLE = True
except ImportError:
    PYLON_AVAILABLE = False
    print("pypylon not available — camera disabled")
```

### Replace `camera_thread()` (lines 195–250)

**Old — Basler/pypylon:**
```python
def camera_thread():
    global latest_frame
    if not PYLON_AVAILABLE:
        print("Camera: pypylon not available, skipping")
        return

    camera = None
    while camera is None:
        try:
            devices = pylon.TlFactory.GetInstance().EnumerateDevices()
            if len(devices) == 0:
                time.sleep(3)
                continue
            camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
            camera.Open()
            print(f"Camera connected: {camera.GetDeviceInfo().GetModelName()}")
        except Exception as e:
            print(f"Camera open failed: {e}")
            camera = None
            time.sleep(3)

    try:
        camera.ExposureAuto.SetValue("Continuous")
    except Exception:
        pass
    try:
        camera.GainAuto.SetValue("Continuous")
    except Exception:
        pass

    camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

    print("Camera grabbing started.")
    while camera.IsGrabbing():
        try:
            grab = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
            if grab.GrabSucceeded():
                image = converter.Convert(grab)
                frame = image.GetArray()
                frame = cv2.resize(frame, (960, 540))
                _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                with frame_lock:
                    latest_frame = jpeg.tobytes()
            grab.Release()
        except Exception as e:
            print(f"Camera grab error: {e}")
            time.sleep(0.1)

    camera.StopGrabbing()
    camera.Close()
```

**New — exploreHD/OpenCV:**
```python
def camera_thread():
    global latest_frame

    cap = None
    while cap is None or not cap.isOpened():
        cap = cv2.VideoCapture(0)  # 0 = first USB camera; try 1 or 2 if wrong device
        if not cap.isOpened():
            print("Camera: not found, retrying in 3s...")
            time.sleep(3)

    print("Camera connected.")
    while True:
        ret, frame = cap.read()
        if not ret:
            print("Camera: lost connection, retrying...")
            cap.release()
            cap = cv2.VideoCapture(0)
            time.sleep(1)
            continue

        frame = cv2.resize(frame, (960, 540))
        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        with frame_lock:
            latest_frame = jpeg.tobytes()
        time.sleep(0.033)
```

### No changes needed
- `generate_frames()` — untouched
- `/video_feed` route — untouched
- `/snapshot` route — untouched
- All serial/Teensy code — untouched

---

## 2. `requirements.txt`

**Remove:**
```
pypylon
```

**Result:**
```
flask==2.3.3
flask-socketio==5.3.6
python-socketio==5.10.0
python-engineio==4.8.0
pyserial
opencv-python-headless
```

---

## 3. `Dockerfile`

**Remove** the `libusb` apt install block (it was only needed for the Basler SDK):
```dockerfile
# Remove this entire block:
# libusb is required for Basler USB3 cameras via pypylon
RUN apt-get update && apt-get install -y --no-install-recommends \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
```

**Result:**
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY templates/ templates/

EXPOSE 8080

CMD ["python", "server.py"]
```

> Note: The exploreHD uses the standard Linux UVC kernel driver — no extra apt packages needed.

---

## Hardware Pre-check

Before running the refactored code, confirm the camera device index on the Jetson Nano:

```bash
ls /dev/video*
```

If multiple devices appear (e.g. `/dev/video0`, `/dev/video1`), identify the exploreHD:

```bash
v4l2-ctl --list-devices
```

Update `cv2.VideoCapture(0)` to the correct index if needed.

---

## Testing After Refactor

1. Rebuild the Docker image: `docker compose build`
2. Run: `docker compose up`
3. Open `http://<jetson-ip>:8080` in a browser
4. Confirm the live video feed appears in the HUD
5. Confirm `/snapshot` returns a JPEG frame

---

## What Does NOT Change

- MJPEG over HTTP streaming protocol
- `/video_feed` and `/snapshot` endpoints
- `generate_frames()` logic
- `_make_placeholder()` fallback (still works if camera not found)
- All Teensy serial communication
- Docker port mapping (8080)
- `docker-compose.yml`
