"""
Web UI for robot control.

Replaces the terminal-only agent with a browser-based interface featuring
a live MJPEG camera feed, mission input, AI chat log, and manual controls.

Usage:
    export GOOGLE_API_KEY="..."
    python web_app.py --depth-url "https://..."

Opens at http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import cv2
import httpx
import serial
from PIL import Image
from pypylon import pylon
from google import genai
from google.genai import types
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, StreamingResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
import uvicorn

logger = logging.getLogger("web_app")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERIAL_PORT = "/dev/cu.usbmodem178421801"
BAUD_RATE = 115200
GEMINI_MODEL = "gemini-3-flash-preview"
MAX_HISTORY_MESSAGES = 40
IMAGE_KEEP_LAST = 2

DIRECTION_MAP: dict[str, str] = {
    "forward": "w",
    "back": "s",
    "left": "a",
    "right": "d",
}

SYSTEM_PROMPT = """You are an autonomous robot controller. You receive a camera image every turn showing your current view.
The image is split: left half = RGB camera, right half = depth map (warm/bright = close, cool/dark = far).

Available tools:
- execute_move: Move the robot. Directions: forward, back, left, right. Duration in seconds (0.1-5.0).
- finish_mission: Call when the mission objective is achieved.

Strategy:
1. Analyze the image you receive and describe what you see (think out loud).
2. Decide on a move and execute it.
3. You will automatically receive a new image after each move.
4. Repeat until the mission is complete, then call finish_mission.

Be cautious — prefer short moves (0.5-1.0s) and observe the result each turn."""

TOOL_DECLARATIONS = types.Tool(function_declarations=[
    {
        "name": "execute_move",
        "description": "Drive the robot in a given direction for a set duration.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "description": "forward, back, left, or right"},
                "duration": {"type": "number", "description": "Seconds to move (0.1-5.0)"},
            },
            "required": ["direction", "duration"],
        },
    },
    {
        "name": "finish_mission",
        "description": "Signal that the current mission is complete.",
        "parameters": {"type": "object", "properties": {}},
    },
])

# ---------------------------------------------------------------------------
# Shared State
# ---------------------------------------------------------------------------


@dataclass
class SharedState:
    camera: pylon.InstantCamera | None = None
    converter: pylon.ImageFormatConverter | None = None
    ser: serial.Serial | None = None
    depth_url: str = ""
    serial_port: str = DEFAULT_SERIAL_PORT
    latest_frame: bytes | None = None  # most recent JPEG
    frame_lock: threading.Lock = field(default_factory=threading.Lock)
    ws_clients: set = field(default_factory=set)
    agent_task: asyncio.Task | None = None
    agent_cancel: asyncio.Event = field(default_factory=asyncio.Event)
    water_value: int = 1023  # analog sensor reading (< 400 = water detected)
    water_detected: bool = False
    distance_mm: int = 0  # ultrasonic range sensor in mm
    event_loop: asyncio.AbstractEventLoop | None = None


shared = SharedState()

# ---------------------------------------------------------------------------
# Camera Thread
# ---------------------------------------------------------------------------


def camera_loop() -> None:
    """Daemon thread: continuously grab frames from Basler camera."""
    assert shared.camera is not None
    assert shared.converter is not None
    logger.info("Camera thread started.")
    while shared.camera.IsGrabbing():
        try:
            grab = shared.camera.RetrieveResult(2000, pylon.TimeoutHandling_ThrowException)
            if grab.GrabSucceeded():
                image = shared.converter.Convert(grab)
                frame = image.GetArray()
                frame = cv2.resize(frame, (960, 540))
                _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                with shared.frame_lock:
                    shared.latest_frame = jpeg.tobytes()
            grab.Release()
        except Exception as exc:
            logger.warning("Camera grab error: %s", exc)
            time.sleep(0.1)
        time.sleep(0.05)  # ~20fps cap
    logger.info("Camera thread exiting.")


# ---------------------------------------------------------------------------
# Serial Reader Thread (water sensor)
# ---------------------------------------------------------------------------


def serial_reader_loop() -> None:
    """Daemon thread: read serial lines from Teensy for sensor data."""
    logger.info("Serial reader thread started.")
    while shared.ser and shared.ser.is_open:
        try:
            line = shared.ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            # Distance sensor: "Distance: 1234 mm"
            if line.startswith("Distance:"):
                try:
                    val = int(line.split(":")[1].strip().replace("mm", "").strip())
                    shared.distance_mm = val
                    if shared.event_loop:
                        asyncio.run_coroutine_threadsafe(
                            broadcast({"type": "distance", "value": val}),
                            shared.event_loop,
                        )
                except ValueError:
                    pass

            # Water warning: ">>> WATER WARNING! Value: 123"
            elif "WATER WARNING" in line:
                try:
                    val = int(line.split("Value:")[1].strip())
                    was_detected = shared.water_detected
                    shared.water_value = val
                    shared.water_detected = True
                    if shared.event_loop:
                        asyncio.run_coroutine_threadsafe(
                            broadcast({"type": "water", "value": val, "detected": True}),
                            shared.event_loop,
                        )
                    if not was_detected:
                        logger.warning("WATER DETECTED! Sensor=%d", val)
                except (ValueError, IndexError):
                    pass

            # No water warning for a while → clear it (check periodically)
            # Water clears implicitly when no WARNING lines come in

        except Exception as exc:
            logger.warning("Serial read error: %s", exc)
            time.sleep(0.5)
    logger.info("Serial reader thread exiting.")


# ---------------------------------------------------------------------------
# MJPEG Stream
# ---------------------------------------------------------------------------


async def mjpeg_generator():
    """Yield multipart JPEG frames for the MJPEG stream."""
    while True:
        with shared.frame_lock:
            frame = shared.latest_frame
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        await asyncio.sleep(0.066)  # ~15fps


async def feed(request):
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------------
# Depth API helper
# ---------------------------------------------------------------------------


async def get_depth_composite(jpeg_bytes: bytes) -> str:
    """POST JPEG to Depth API, return base64 composite. Falls back to raw."""
    logger.info("get_depth_composite called, depth_url=%r", shared.depth_url)
    if not shared.depth_url:
        return base64.b64encode(jpeg_bytes).decode()
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                shared.depth_url,
                files={"image": ("frame.jpg", jpeg_bytes, "image/jpeg")},
            )
        if resp.status_code == 200:
            data = resp.json()
            logger.info("Depth: %.2fm–%.2fm", data.get("depth_min_m", 0), data.get("depth_max_m", 0))
            return data["composite_b64"]
    except Exception as exc:
        logger.warning("Depth API failed: %r", exc, exc_info=True)
    return base64.b64encode(jpeg_bytes).decode()


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------


def serial_send(cmd: str) -> None:
    if shared.ser:
        shared.ser.write(cmd.encode())


def emergency_stop() -> None:
    serial_send("x")
    shared.agent_cancel.set()
    if shared.agent_task and not shared.agent_task.done():
        shared.agent_task.cancel()
    logger.info("EMERGENCY STOP")


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------


async def broadcast(msg: dict) -> None:
    """Send a JSON message to all connected WebSocket clients."""
    text = json.dumps(msg)
    dead = set()
    for ws in shared.ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    shared.ws_clients -= dead


# ---------------------------------------------------------------------------
# WebRobotAgent
# ---------------------------------------------------------------------------


class WebRobotAgent:
    """Observe-think-act loop that broadcasts events over WebSocket."""

    def __init__(self, mission: str) -> None:
        self.mission = mission
        self.contents: list[types.Content] = []
        self.finished = False
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Set GOOGLE_API_KEY environment variable.")
        self.client = genai.Client(api_key=api_key)

    async def run(self) -> None:
        await broadcast({"type": "status", "text": f"Mission started: {self.mission}"})
        shared.agent_cancel.clear()

        try:
            while not self.finished and not shared.agent_cancel.is_set():
                await self._step()
        except asyncio.CancelledError:
            await broadcast({"type": "status", "text": "Mission cancelled."})
        except Exception as exc:
            logger.exception("Agent error")
            await broadcast({"type": "error", "text": str(exc)})

        serial_send("x")
        await broadcast({"type": "status", "text": "Mission ended."})

    async def _step(self) -> None:
        # 1. Capture frame
        with shared.frame_lock:
            jpeg = shared.latest_frame
        if not jpeg:
            await asyncio.sleep(0.5)
            return

        # 2. Get depth composite
        observation_b64 = await get_depth_composite(jpeg)

        # Send depth composite thumbnail to UI (downscale to keep WebSocket fast)
        try:
            composite_bytes = base64.b64decode(observation_b64)
            img = Image.open(io.BytesIO(composite_bytes))
            img.thumbnail((640, 360))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60)
            thumb_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            thumb_b64 = base64.b64encode(jpeg).decode()
        await broadcast({"type": "observation", "image": thumb_b64})

        # 3. Build user message
        user_parts = self._build_parts(observation_b64)
        self.contents.append(types.Content(role="user", parts=user_parts))
        self.contents = self._trim_history(self.contents)

        # 4. Call Gemini (sync SDK → run in thread)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[TOOL_DECLARATIONS],
            temperature=1.0,
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
        )
        response = None
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=self.contents,
                    config=config,
                )
                break
            except Exception as exc:
                logger.warning("Gemini attempt %d failed: %s", attempt + 1, exc)
                await broadcast({"type": "status", "text": f"Gemini error (retry {attempt + 1}/3)..."})
                if attempt < 2:
                    await asyncio.sleep(2)
        if response is None:
            await broadcast({"type": "error", "text": "Gemini failed after 3 retries, skipping step."})
            self.contents.pop()  # remove the unanswered user message
            return

        # 5. Process response
        candidate = response.candidates[0].content
        self.contents.append(candidate)

        function_response_parts = []
        for i, part in enumerate(candidate.parts):
            logger.info("Part %d: thought=%s, has_text=%s, has_fc=%s, type=%s",
                        i, getattr(part, "thought", None),
                        bool(getattr(part, "text", None)),
                        bool(getattr(part, "function_call", None)),
                        type(part).__name__)

            if getattr(part, "thought", False):
                if getattr(part, "text", None):
                    logger.info("Thinking: %s", part.text[:200])
                    await broadcast({"type": "ai_text", "text": f"[Thinking] {part.text}"})
                continue

            if getattr(part, "text", None):
                logger.info("Agent: %s", part.text)
                await broadcast({"type": "ai_text", "text": part.text})

            if getattr(part, "function_call", None):
                fc = part.function_call
                result = await self._execute_tool(fc.name, dict(fc.args))
                await broadcast({
                    "type": "action",
                    "tool": fc.name,
                    "args": dict(fc.args),
                    "result": result,
                })
                function_response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    )
                )

        if function_response_parts:
            self.contents.append(types.Content(role="user", parts=function_response_parts))

    def _build_parts(self, observation_b64: str) -> list[types.Part]:
        parts: list[types.Part] = []
        if not self.contents:
            parts.append(types.Part(text=f"Your mission: {self.mission}"))
        parts.append(types.Part(text="Here is your current camera view:"))
        if observation_b64:
            image_bytes = base64.b64decode(observation_b64)
            mime = "image/png" if image_bytes[:4] == b'\x89PNG' else "image/jpeg"
            parts.append(types.Part(inline_data=types.Blob(mime_type=mime, data=image_bytes)))
        else:
            parts.append(types.Part(text="[no image available]"))
        parts.append(types.Part(text="What do you see? Decide your next action."))
        return parts

    async def _execute_tool(self, name: str, args: dict) -> str:
        if name == "execute_move":
            direction = args.get("direction", "").lower().strip()
            duration = float(args.get("duration", 1.0))
            if direction not in DIRECTION_MAP:
                return f"Invalid direction '{direction}'."
            duration = max(0.1, min(duration, 5.0))
            serial_send(DIRECTION_MAP[direction])
            await asyncio.sleep(duration)
            serial_send("x")
            return f"Moved {direction} for {duration:.1f}s."
        elif name == "finish_mission":
            self.finished = True
            serial_send("x")
            return "Mission complete. Robot stopped."
        return f"Unknown tool: {name}"

    def _trim_history(self, contents: list[types.Content]) -> list[types.Content]:
        image_indices = [
            i for i, c in enumerate(contents)
            if c.parts and any(getattr(p, "inline_data", None) for p in c.parts)
        ]
        for i in image_indices[:-IMAGE_KEEP_LAST]:
            new_parts = []
            for p in contents[i].parts:
                if getattr(p, "inline_data", None):
                    new_parts.append(types.Part(text="[image removed]"))
                else:
                    new_parts.append(p)
            contents[i] = types.Content(role=contents[i].role, parts=new_parts)
        if len(contents) > MAX_HISTORY_MESSAGES:
            contents = contents[-MAX_HISTORY_MESSAGES:]
        return contents


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def homepage(request):
    return HTMLResponse(HTML_PAGE)


async def api_stop(request):
    emergency_stop()
    return JSONResponse({"status": "stopped"})


async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    shared.ws_clients.add(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "mission":
                mission = data.get("text", "").strip()
                if not mission:
                    continue
                # Cancel any running agent
                if shared.agent_task and not shared.agent_task.done():
                    shared.agent_cancel.set()
                    shared.agent_task.cancel()
                    await asyncio.sleep(0.3)

                agent = WebRobotAgent(mission)
                shared.agent_task = asyncio.create_task(agent.run())

            elif msg_type == "stop":
                emergency_stop()
                await broadcast({"type": "status", "text": "Stopped."})

            elif msg_type == "manual":
                direction = data.get("direction", "")
                if direction in DIRECTION_MAP:
                    serial_send(DIRECTION_MAP[direction])
                    await asyncio.sleep(0.4)
                    serial_send("x")
                    await broadcast({"type": "action", "tool": "manual", "args": {"direction": direction}, "result": f"Manual {direction}"})

    except WebSocketDisconnect:
        pass
    finally:
        shared.ws_clients.discard(websocket)


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------


async def startup():
    logger.info("Starting up...")
    shared.event_loop = asyncio.get_event_loop()

    # Serial
    try:
        shared.ser = serial.Serial(shared.serial_port, BAUD_RATE, timeout=0.5)
        logger.info("Serial port opened: %s", shared.serial_port)
        # Start serial reader for water sensor
        sr = threading.Thread(target=serial_reader_loop, daemon=True)
        sr.start()
    except serial.SerialException as e:
        logger.warning("No serial: %s", e)

    # Camera
    try:
        camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
        camera.Open()
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

        # Warmup
        time.sleep(2)
        for _ in range(50):
            g = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
            g.Release()

        shared.camera = camera
        shared.converter = converter

        t = threading.Thread(target=camera_loop, daemon=True)
        t.start()
        logger.info("Camera ready: %s", camera.GetDeviceInfo().GetModelName())
    except Exception as exc:
        logger.error("Camera init failed: %s", exc)


async def shutdown():
    logger.info("Shutting down...")
    if shared.agent_task and not shared.agent_task.done():
        shared.agent_task.cancel()
    serial_send("x")
    if shared.ser:
        shared.ser.close()
    if shared.camera:
        shared.camera.StopGrabbing()
        shared.camera.Close()


# ---------------------------------------------------------------------------
# HTML Page
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Robot Control Center</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a2e; color: #e0e0e0; font-family: 'Segoe UI', system-ui, sans-serif; height: 100vh; display: flex; flex-direction: column; }

  header { display: flex; justify-content: space-between; align-items: center; padding: 12px 20px; background: #16213e; border-bottom: 1px solid #0f3460; }
  header h1 { font-size: 1.3rem; color: #e94560; }
  .header-right { display: flex; align-items: center; gap: 14px; }
  .sensor-badge { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; padding: 6px 12px; border-radius: 6px; }
  #dist-indicator { background: #1a1a3e; border: 1px solid #533483; color: #c0c0ff; }
  #dist-indicator .dist-val { font-weight: 700; font-variant-numeric: tabular-nums; min-width: 50px; text-align: right; }
  #dist-indicator.close { border-color: #e94560; color: #ff8a8a; }
  #water-indicator { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; padding: 6px 12px; border-radius: 6px; background: #1a2e1a; border: 1px solid #4caf50; }
  #water-indicator.alert { background: #3e1a1a; border-color: #e94560; animation: pulse 1s infinite; }
  #water-dot { width: 10px; height: 10px; border-radius: 50%; background: #4caf50; }
  #water-indicator.alert #water-dot { background: #e94560; }
  
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
  #estop { background: #e94560; color: white; border: none; padding: 8px 20px; border-radius: 6px; font-weight: bold; font-size: 1rem; cursor: pointer; }
  #estop:hover { background: #c73650; }

  .main { display: flex; flex: 1; overflow: hidden; }

  .feed-panel { flex: 1; display: flex; align-items: center; justify-content: center; background: #0d0d1a; min-width: 0; }
  .feed-panel img { max-width: 100%; max-height: 100%; object-fit: contain; }

  .chat-panel { width: 420px; display: flex; flex-direction: column; border-left: 1px solid #0f3460; background: #16213e; }
  .chat-header { padding: 10px 14px; font-weight: 600; border-bottom: 1px solid #0f3460; font-size: 0.9rem; color: #a0a0c0; }
  #chatlog { flex: 1; overflow-y: auto; padding: 10px 14px; display: flex; flex-direction: column; gap: 8px; }

  .msg { padding: 8px 10px; border-radius: 8px; font-size: 0.85rem; line-height: 1.4; word-wrap: break-word; }
  .msg.ai { background: #1a1a3e; border-left: 3px solid #533483; }
  .msg.status { background: #0f3460; color: #a0c4ff; font-style: italic; }
  .msg.action { background: #1a2e1a; border-left: 3px solid #4caf50; }
  .msg.error { background: #3e1a1a; border-left: 3px solid #e94560; }
  .msg.observation { background: #1a1a2e; }
  .msg.observation img { max-width: 100%; border-radius: 4px; margin-top: 4px; }

  .action-badge { display: inline-block; background: #4caf50; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; font-weight: 600; margin-right: 6px; }

  footer { padding: 12px 20px; background: #16213e; border-top: 1px solid #0f3460; }
  .mission-row { display: flex; gap: 8px; margin-bottom: 10px; }
  #mission-input { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid #0f3460; background: #1a1a2e; color: #e0e0e0; font-size: 0.95rem; outline: none; }
  #mission-input:focus { border-color: #533483; }
  .btn { padding: 10px 18px; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 0.9rem; }
  .btn-send { background: #533483; color: white; }
  .btn-send:hover { background: #6a45a0; }
  .btn-stop { background: #e94560; color: white; }
  .btn-stop:hover { background: #c73650; }

  .controls-row { display: flex; gap: 6px; align-items: center; }
  .controls-label { font-size: 0.8rem; color: #a0a0c0; margin-right: 8px; }
  .btn-manual { width: 42px; height: 42px; border-radius: 8px; border: 1px solid #0f3460; background: #1a1a2e; color: #e0e0e0; font-weight: bold; font-size: 1rem; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .btn-manual:hover { background: #533483; }
  .btn-manual:active { background: #e94560; }

  #chatlog::-webkit-scrollbar { width: 6px; }
  #chatlog::-webkit-scrollbar-track { background: #16213e; }
  #chatlog::-webkit-scrollbar-thumb { background: #533483; border-radius: 3px; }

  /* Water alert popup */
  #water-popup { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: 1000; background: rgba(0,0,0,0.7); align-items: center; justify-content: center; }
  #water-popup.show { display: flex; }
  .water-popup-box { background: #2a1a1a; border: 3px solid #e94560; border-radius: 16px; padding: 40px 50px; text-align: center; animation: popIn 0.3s ease-out; max-width: 420px; }
  .water-popup-box .icon { font-size: 4rem; margin-bottom: 12px; }
  .water-popup-box h2 { color: #e94560; font-size: 1.8rem; margin-bottom: 8px; animation: pulse 1s infinite; }
  .water-popup-box .val { color: #ff8a8a; font-size: 1.1rem; margin-bottom: 20px; }
  .water-popup-box button { background: #e94560; color: white; border: none; padding: 10px 30px; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; }
  .water-popup-box button:hover { background: #c73650; }
  @keyframes popIn { from { transform: scale(0.8); opacity: 0; } to { transform: scale(1); opacity: 1; } }
</style>
</head>
<body>

<header>
  <h1>Robot Control Center</h1>
  <div class="header-right">
    <div id="dist-indicator" class="sensor-badge">
      <span>Range:</span>
      <span class="dist-val" id="dist-val">--</span>
      <span>mm</span>
    </div>
    <div id="water-indicator">
      <span id="water-dot"></span>
      <span>Water: <span id="water-val">--</span></span>
    </div>
    <button id="estop" onclick="estop()">E-STOP</button>
  </div>
</header>

<div class="main">
  <div class="feed-panel">
    <img src="/feed" alt="Live Camera Feed">
  </div>
  <div class="chat-panel">
    <div class="chat-header">AI Chat Log</div>
    <div id="chatlog"></div>
  </div>
</div>

<footer>
  <div class="mission-row">
    <input id="mission-input" type="text" placeholder="Enter mission objective..." autocomplete="off">
    <button class="btn btn-send" onclick="sendMission()">Send</button>
    <button class="btn btn-stop" onclick="sendStop()">Stop</button>
  </div>
  <div class="controls-row">
    <span class="controls-label">Manual:</span>
    <button class="btn-manual" onclick="manual('forward')">W</button>
    <button class="btn-manual" onclick="manual('left')">A</button>
    <button class="btn-manual" onclick="manual('back')">S</button>
    <button class="btn-manual" onclick="manual('right')">D</button>
  </div>
</footer>

<div id="water-popup">
  <div class="water-popup-box">
    <div class="icon">💧</div>
    <h2>WATER DETECTED</h2>
    <div class="val">Sensor: <span id="popup-water-val">0</span></div>
    <button onclick="dismissWater()">Dismiss</button>
  </div>
</div>

<script>
const chatlog = document.getElementById('chatlog');
const missionInput = document.getElementById('mission-input');
let ws;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => addMsg('status', 'Connected to server.');
  ws.onclose = () => { addMsg('status', 'Disconnected. Reconnecting...'); setTimeout(connect, 2000); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ai_text') {
      addMsg('ai', msg.text);
    } else if (msg.type === 'status') {
      addMsg('status', msg.text);
    } else if (msg.type === 'error') {
      addMsg('error', msg.text);
    } else if (msg.type === 'action') {
      const badge = msg.tool + (msg.args ? ' ' + JSON.stringify(msg.args) : '');
      addMsg('action', '<span class="action-badge">' + escHtml(msg.tool) + '</span> ' + escHtml(msg.result || ''));
    } else if (msg.type === 'distance') {
      document.getElementById('dist-val').textContent = msg.value;
      const el = document.getElementById('dist-indicator');
      if (msg.value < 200) { el.classList.add('close'); } else { el.classList.remove('close'); }
    } else if (msg.type === 'water') {
      const el = document.getElementById('water-indicator');
      document.getElementById('water-val').textContent = msg.value;
      if (msg.detected) {
        el.classList.add('alert');
        el.querySelector('span:last-child').innerHTML = 'WATER: <span id="water-val">' + msg.value + '</span>';
        document.getElementById('popup-water-val').textContent = msg.value;
        document.getElementById('water-popup').classList.add('show');
      } else {
        el.classList.remove('alert');
        el.querySelector('span:last-child').innerHTML = 'Water: <span id="water-val">' + msg.value + '</span>';
        document.getElementById('water-popup').classList.remove('show');
      }
    } else if (msg.type === 'observation') {
      const div = document.createElement('div');
      div.className = 'msg observation';
      div.innerHTML = '<small style="color:#a0a0c0">Observation sent to AI:</small><br><img src="data:image/jpeg;base64,' + msg.image + '">';
      chatlog.appendChild(div);
      chatlog.scrollTop = chatlog.scrollHeight;
    }
  };
}

function addMsg(cls, html) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.innerHTML = html;
  chatlog.appendChild(div);
  chatlog.scrollTop = chatlog.scrollHeight;
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function sendMission() {
  const text = missionInput.value.trim();
  if (!text || !ws) return;
  ws.send(JSON.stringify({type: 'mission', text}));
  addMsg('status', 'Mission: ' + escHtml(text));
  missionInput.value = '';
}

function sendStop() {
  if (ws) ws.send(JSON.stringify({type: 'stop'}));
}

function estop() {
  fetch('/api/stop', {method: 'POST'});
  if (ws) ws.send(JSON.stringify({type: 'stop'}));
  addMsg('error', 'EMERGENCY STOP');
}

function manual(dir) {
  if (ws) ws.send(JSON.stringify({type: 'manual', direction: dir}));
}

function dismissWater() {
  document.getElementById('water-popup').classList.remove('show');
}

missionInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendMission();
});

// Keyboard shortcuts for WASD
document.addEventListener('keydown', (e) => {
  if (document.activeElement === missionInput) return;
  const map = {w: 'forward', a: 'left', s: 'back', d: 'right'};
  if (map[e.key]) { e.preventDefault(); manual(map[e.key]); }
  if (e.key === ' ') { e.preventDefault(); sendStop(); }
});

connect();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/feed", feed),
        Route("/api/stop", api_stop, methods=["POST"]),
        WebSocketRoute("/ws", websocket_endpoint),
    ],
    on_startup=[startup],
    on_shutdown=[shutdown],
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Robot Web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--serial-port", default=DEFAULT_SERIAL_PORT, help="Teensy serial port")
    parser.add_argument("--depth-url", default="", help="Modal Depth Pro API URL")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    shared.serial_port = args.serial_port
    shared.depth_url = args.depth_url

    logger.info("depth_url = %s", shared.depth_url)

    # Pass app as string would re-import and lose shared state.
    # Passing the object directly preserves it.
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
