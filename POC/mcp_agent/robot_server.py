"""
MCP Server for the 2-wheeled robot.

Exposes tools for observation, movement, and mission control.
Communicates with the Teensy over serial using the same protocol as test.py.
Captures frames from a Basler USB camera and optionally sends them to a
Modal GPU endpoint for Depth Pro inference.

Usage:
    python robot_server.py
    python robot_server.py --depth-url "https://YOUR--depth-pro-api-predict.modal.run"
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import io
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import cv2
import httpx
import serial
from PIL import Image
from pypylon import pylon
from mcp.server.fastmcp import FastMCP, Context

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERIAL_PORT = "/dev/cu.usbmodem178421801"
BAUD_RATE = 115200
DEFAULT_DEPTH_URL = ""  # set via --depth-url or leave empty to skip depth

DIRECTION_MAP: dict[str, str] = {
    "forward": "w",
    "back": "s",
    "left": "a",
    "right": "d",
}

logger = logging.getLogger("robot_server")

# ---------------------------------------------------------------------------
# Basler camera capture
# ---------------------------------------------------------------------------


def _open_basler_camera() -> tuple[pylon.InstantCamera, pylon.ImageFormatConverter]:
    """Open the first available Basler USB camera and return (camera, converter)."""
    camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
    camera.Open()

    # Enable auto exposure and auto gain
    try:
        camera.ExposureAuto.SetValue("Continuous")
    except Exception:
        logger.warning("Could not set auto exposure")
    try:
        camera.GainAuto.SetValue("Continuous")
    except Exception:
        logger.warning("Could not set auto gain")

    camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

    logger.info("Basler camera opened: %s", camera.GetDeviceInfo().GetModelName())

    # Warmup: discard initial black frames while auto exposure adjusts
    time.sleep(2)
    for _ in range(50):
        grab = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
        grab.Release()
    logger.info("Camera warmup complete.")

    return camera, converter


def _capture_frame(camera: pylon.InstantCamera, converter: pylon.ImageFormatConverter) -> bytes | None:
    """Grab a single JPEG frame from the Basler camera. Returns raw bytes."""
    if not camera.IsGrabbing():
        logger.error("Basler camera is not grabbing.")
        return None

    grab_result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
    try:
        if not grab_result.GrabSucceeded():
            logger.error("Basler grab failed: %s", grab_result.ErrorDescription)
            return None

        image = converter.Convert(grab_result)
        frame = image.GetArray()
        logger.info("Raw frame: shape=%s, min=%d, max=%d", frame.shape, frame.min(), frame.max())
        # Resize to 960x540 to keep image size manageable for LLM
        frame = cv2.resize(frame, (960, 540))
        # Save debug frame
        cv2.imwrite("/tmp/basler_debug.jpg", frame)
        _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        logger.info("JPEG encoded: %d bytes", len(jpeg.tobytes()))
        return jpeg.tobytes()
    finally:
        grab_result.Release()


async def _get_depth_composite(jpeg_bytes: bytes, depth_url: str) -> str:
    """POST a JPEG to the Modal Depth Pro endpoint, return composite base64.

    If depth_url is empty, just returns the raw frame as base64 (no depth).
    """
    if not depth_url:
        return base64.b64encode(jpeg_bytes).decode()

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            depth_url,
            files={"image": ("frame.jpg", jpeg_bytes, "image/jpeg")},
        )

    if resp.status_code != 200:
        logger.warning("Depth API returned %s: %s", resp.status_code, resp.text)
        return base64.b64encode(jpeg_bytes).decode()

    data = resp.json()
    logger.info(
        "Depth: %.2fm–%.2fm (%.3fs)",
        data.get("depth_min_m", 0),
        data.get("depth_max_m", 0),
        data.get("inference_s", 0),
    )
    return data["composite_b64"]


# ---------------------------------------------------------------------------
# Shared robot state
# ---------------------------------------------------------------------------


@dataclass
class RobotState:
    ser: serial.Serial | None
    camera: pylon.InstantCamera
    converter: pylon.ImageFormatConverter
    depth_url: str
    mission_finished: bool = False


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[RobotState]:
    """Open serial and Basler camera on startup, clean up on shutdown."""
    port = server._config.get("serial_port", DEFAULT_SERIAL_PORT)
    depth_url = server._config.get("depth_url", DEFAULT_DEPTH_URL)

    ser = None
    try:
        ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
        logger.info("Serial port %s opened.", port)
    except serial.SerialException as e:
        logger.warning("Could not open serial port %s: %s — running without Teensy.", port, e)

    camera, converter = _open_basler_camera()

    if depth_url:
        logger.info("Depth API: %s", depth_url)
    else:
        logger.info("No depth API configured — returning RGB only.")

    state = RobotState(ser=ser, camera=camera, converter=converter, depth_url=depth_url)
    try:
        yield state
    finally:
        if ser:
            ser.write(b"x")
            ser.close()
        camera.StopGrabbing()
        camera.Close()
        logger.info("Camera closed.")


mcp = FastMCP("RobotServer", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_observation(ctx: Context) -> str:
    """Capture a camera frame and depth map from the robot.

    Returns a base64-encoded JPEG. If a depth API is configured, the image
    is a side-by-side composite: left = RGB camera, right = depth map
    (warm/bright = close, cool/dark = far).
    """
    state: RobotState = ctx.request_context.lifespan_context

    jpeg_bytes = _capture_frame(state.camera, state.converter)
    if jpeg_bytes is None:
        return ""

    logger.info("Captured frame: %d bytes", len(jpeg_bytes))
    try:
        result = await _get_depth_composite(jpeg_bytes, state.depth_url)
        logger.info("Returning image: %d chars base64", len(result))
        return result
    except Exception as exc:
        logger.warning("Depth API call failed (%s), returning RGB only.", exc)
        return base64.b64encode(jpeg_bytes).decode()


@mcp.tool()
async def execute_move(direction: str, duration: float, ctx: Context) -> str:
    """Drive the robot in a given direction for a set duration.

    Args:
        direction: One of 'forward', 'back', 'left', 'right'.
        duration:  Seconds to move (clamped 0.1–5.0).
    """
    state: RobotState = ctx.request_context.lifespan_context

    direction = direction.lower().strip()
    if direction not in DIRECTION_MAP:
        return f"Invalid direction '{direction}'. Use forward/back/left/right."

    duration = max(0.1, min(duration, 5.0))
    cmd = DIRECTION_MAP[direction]

    if state.ser:
        state.ser.write(cmd.encode())
    await asyncio.sleep(duration)
    if state.ser:
        state.ser.write(b"x")  # stop

    return f"Moved {direction} for {duration:.1f}s."


@mcp.tool()
async def finish_mission(ctx: Context) -> str:
    """Signal that the current mission is complete."""
    state: RobotState = ctx.request_context.lifespan_context
    state.mission_finished = True
    if state.ser:
        state.ser.write(b"x")
    logger.info("Mission complete.")
    return "Mission complete. Robot stopped."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Robot MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--port", default=DEFAULT_SERIAL_PORT, help="Teensy serial port")
    parser.add_argument("--depth-url", default=DEFAULT_DEPTH_URL,
                        help="Modal Depth Pro API URL (omit to skip depth)")
    args = parser.parse_args()

    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", stream=sys.stderr)
    mcp._config = {
        "serial_port": args.port,
        "depth_url": args.depth_url,
    }
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
