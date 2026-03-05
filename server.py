import os
import sys
import time
import threading
import serial
import serial.tools.list_ports
import cv2
from flask import Flask, render_template, Response
from flask_socketio import SocketIO, emit
#dylan 22
try:
    from pypylon import pylon
    PYLON_AVAILABLE = True
except ImportError:
    PYLON_AVAILABLE = False
    print("pypylon not available — camera disabled")

BAUD_RATE = int(os.environ.get('BAUD_RATE', '115200'))
TEENSY_VID = 0x16C0  # PJRC (Teensy) USB vendor ID


def find_teensy():
    """Return the serial port path for the first Teensy found, or None."""
    ports = serial.tools.list_ports.comports()
    # Try VID match first (works on bare metal)
    for port in ports:
        if port.vid == TEENSY_VID:
            return port.device
    # Fallback: first ttyACM device (VID unavailable inside Docker)
    for port in ports:
        if 'ttyACM' in port.device:
            print(f"Teensy VID not found, falling back to {port.device}")
            return port.device
    return None

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- State ---
state = {
    'direction': 'STOPPED',
    'speed': 5,
    'speed_min': 1,
    'speed_max': 10,
    'impeller_power': 0,
    'brush_on': False,
    'light_on': False,
    'impeller_read_power': 0,
    'impeller_read_pwm': 1500,
    'water_warning': False,
    'water_value': 0,
    'distance_mm': 0,
    'teensy_connected': False,
    'maneuver_direction': 'STOPPED',
    'maneuver_power': 50,  # matches Teensy default (50%)
}

IMPELLER_STEP = 10
water_warn_time = 0
ser = None

# --- Camera ---
latest_frame = None
frame_lock = threading.Lock()


def connect_serial():
    """Background thread: keep scanning for a Teensy and (re)connect whenever found."""
    global ser
    while True:
        if ser is not None and ser.is_open:
            time.sleep(2)
            continue

        state['teensy_connected'] = False
        port = find_teensy()
        if port is None:
            print("Teensy not found — scanning...")
            time.sleep(2)
            continue

        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
            state['teensy_connected'] = True
            print(f"Teensy connected on {port}")
        except serial.SerialException as e:
            print(f"Could not open {port}: {e}")
            ser = None
            time.sleep(2)


def read_telemetry():
    global water_warn_time, ser
    while True:
        if ser is None or not ser.is_open:
            socketio.emit('state', state)
            time.sleep(1)
            continue
        try:
            while ser.in_waiting:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                if line.startswith("Distance:"):
                    try:
                        state['distance_mm'] = int(line.split(':')[1].strip())
                    except (IndexError, ValueError):
                        pass
                elif line.startswith("Brush Motor:"):
                    state['brush_on'] = "ON" in line
                elif "Water" in line:
                    state['water_warning'] = True
                    water_warn_time = time.time()

            if state['water_warning'] and time.time() - water_warn_time > 2.0:
                state['water_warning'] = False

            socketio.emit('state', state)
            time.sleep(0.1)
        except Exception as e:
            print(f"Telemetry error: {e}")
            ser = None  # let connect_serial loop re-detect
            state['teensy_connected'] = False
            time.sleep(1)


def send_serial(data):
    if ser and ser.is_open:
        ser.write((data + '\n').encode())


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('connect')
def on_connect():
    emit('state', state)


@socketio.on('command')
def on_command(data):
    cmd = data.get('cmd')
    if not cmd:
        return

    if cmd in ('w', 'a', 's', 'd'):
        direction_map = {'w': 'FORWARD', 's': 'REVERSE', 'a': 'LEFT', 'd': 'RIGHT'}
        state['direction'] = direction_map[cmd]
        send_serial(cmd)
    elif cmd == 'x':
        state['direction'] = 'STOPPED'
        send_serial('x')
    elif cmd == '+' and state['speed'] < state['speed_max']:
        state['speed'] += 1
        send_serial('+')
    elif cmd == '-' and state['speed'] > state['speed_min']:
        state['speed'] -= 1
        send_serial('-')
    elif cmd == 'imp_up':
        state['impeller_power'] = min(state['impeller_power'] + IMPELLER_STEP, 100)
        send_serial(str(state['impeller_power']))
        state['impeller_read_power'] = state['impeller_power']
        state['impeller_read_pwm'] = 1500 + (state['impeller_power'] * 5)
    elif cmd == 'imp_down':
        state['impeller_power'] = max(state['impeller_power'] - IMPELLER_STEP, -100)
        send_serial(str(state['impeller_power']))
        state['impeller_read_power'] = state['impeller_power']
        state['impeller_read_pwm'] = 1500 + (state['impeller_power'] * 5)
    elif cmd == 'imp_stop':
        state['impeller_power'] = 0
        send_serial('0')
        state['impeller_read_power'] = 0
        state['impeller_read_pwm'] = 1500
    elif cmd == 'brush':
        state['brush_on'] = not state['brush_on']
        send_serial('brush_on' if state['brush_on'] else 'brush_off')
    elif cmd == 'light':
        state['light_on'] = not state['light_on']
        send_serial('light_on' if state['light_on'] else 'light_off')
    elif cmd in ('man_w', 'man_s', 'man_a', 'man_d'):
        man_dir_map = {'man_w': 'FORWARD', 'man_s': 'REVERSE', 'man_a': 'LEFT', 'man_d': 'RIGHT'}
        state['maneuver_direction'] = man_dir_map[cmd]
        send_serial(cmd)
    elif cmd == 'man_x':
        state['maneuver_direction'] = 'STOPPED'
        send_serial('man_x')
    elif cmd == 'man_spd_up':
        state['maneuver_power'] = min(state['maneuver_power'] + 10, 100)
        send_serial('man_spd_up')
    elif cmd == 'man_spd_down':
        state['maneuver_power'] = max(state['maneuver_power'] - 10, 0)
        send_serial('man_spd_down')

    socketio.emit('state', state)


def camera_thread():
    global latest_frame
    if not PYLON_AVAILABLE:
        print("Camera: pypylon not available, skipping")
        return

    # Retry loop — keep trying until a camera appears
    camera = None
    while camera is None:
        try:
            devices = pylon.TlFactory.GetInstance().EnumerateDevices()
            print(f"Camera: found {len(devices)} device(s)")
            if len(devices) == 0:
                print("Camera: no devices found, retrying in 3s...")
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


def _make_placeholder():
    import numpy as np
    img = np.zeros((540, 960, 3), dtype=np.uint8)
    cv2.putText(img, 'No Camera', (350, 290),
                cv2.FONT_HERSHEY_SIMPLEX, 2.5, (80, 80, 80), 4)
    _, enc = cv2.imencode('.jpg', img)
    return enc.tobytes()


_placeholder = None


def generate_frames():
    global _placeholder
    while True:
        with frame_lock:
            frame = latest_frame
        if frame is None:
            if _placeholder is None:
                _placeholder = _make_placeholder()
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + _placeholder + b'\r\n'
            )
            time.sleep(1)
            continue
        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
        )
        time.sleep(0.033)


@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/snapshot')
def snapshot():
    global _placeholder
    with frame_lock:
        frame = latest_frame
    if frame is None:
        if _placeholder is None:
            _placeholder = _make_placeholder()
        frame = _placeholder
    return Response(frame, mimetype='image/jpeg')


if __name__ == '__main__':
    threading.Thread(target=connect_serial, daemon=True).start()
    threading.Thread(target=read_telemetry, daemon=True).start()
    threading.Thread(target=camera_thread, daemon=True).start()
    print("Starting server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080, allow_unsafe_werkzeug=True)
