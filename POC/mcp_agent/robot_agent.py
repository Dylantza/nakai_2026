"""
Autonomous Agent for the 2-wheeled robot.

Manages the observe-think-act loop via MCP tool calls to robot_server.py.
Every loop iteration fetches a fresh observation and sends it alongside
the conversation history to Gemini 3 Pro — the LLM always sees a fresh frame.

Usage:
    export GOOGLE_API_KEY="your-key-here"
    python robot_agent.py --mission "Navigate to the red box"
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import logging
import os

from google import genai
from google.genai import types

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("robot_agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_MODEL = "gemini-3-pro-preview"
MAX_HISTORY_MESSAGES = 40
IMAGE_KEEP_LAST = 2

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

# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

_gemini_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Set GOOGLE_API_KEY environment variable.")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


# ---------------------------------------------------------------------------
# Tool-schema converter (MCP tools -> Gemini function declarations)
# ---------------------------------------------------------------------------


def mcp_tools_to_gemini(mcp_tools: list) -> types.Tool:
    """Convert MCP tool definitions to a Gemini Tool with FunctionDeclarations.

    Filters out get_observation since the agent handles it automatically.
    """
    declarations = []
    for tool in mcp_tools:
        if tool.name == "get_observation":
            continue
        schema = tool.inputSchema if hasattr(tool, "inputSchema") else {}
        # Strip the top-level keys Gemini doesn't expect inside parameters
        params = {
            "type": schema.get("type", "object"),
            "properties": schema.get("properties", {}),
        }
        if "required" in schema:
            params["required"] = schema["required"]

        declarations.append({
            "name": tool.name,
            "description": tool.description or "",
            "parameters": params,
        })
    return types.Tool(function_declarations=declarations)


# ---------------------------------------------------------------------------
# History / token management
# ---------------------------------------------------------------------------


def trim_history(contents: list[types.Content]) -> list[types.Content]:
    """Remove image data from older messages to save tokens.

    Only the most recent IMAGE_KEEP_LAST user messages with images keep their
    inline_data. Older images are replaced with a text placeholder.
    Also caps total message count at MAX_HISTORY_MESSAGES.
    """
    image_indices: list[int] = []
    for i, content in enumerate(contents):
        if _content_has_image(content):
            image_indices.append(i)

    indices_to_strip = image_indices[:-IMAGE_KEEP_LAST] if len(image_indices) > IMAGE_KEEP_LAST else []
    for i in indices_to_strip:
        contents[i] = _strip_image_from_content(contents[i])

    if len(contents) > MAX_HISTORY_MESSAGES:
        contents = contents[-MAX_HISTORY_MESSAGES:]

    return contents


def _content_has_image(content: types.Content) -> bool:
    if not content.parts:
        return False
    return any(getattr(p, "inline_data", None) is not None for p in content.parts)


def _strip_image_from_content(content: types.Content) -> types.Content:
    """Replace inline_data parts with a text placeholder."""
    new_parts = []
    for part in content.parts:
        if getattr(part, "inline_data", None) is not None:
            new_parts.append(types.Part(text="[image removed to save tokens]"))
        else:
            new_parts.append(part)
    return types.Content(role=content.role, parts=new_parts)


# ---------------------------------------------------------------------------
# Robot Controller
# ---------------------------------------------------------------------------


class RobotController:
    """Manages the autonomous observe-think-act loop."""

    def __init__(self, mission: str) -> None:
        self.mission = mission
        self.contents: list[types.Content] = []
        self.session: ClientSession | None = None
        self.gemini_tool: types.Tool | None = None
        self.finished = False

    async def run(self, server_cmd: list[str]) -> None:
        """Main entry point — connects to MCP and runs the control loop."""
        params = StdioServerParameters(
            command=server_cmd[0],
            args=server_cmd[1:],
        )

        logger.info("Connecting to MCP server...")
        async with stdio_client(params) as (read_stream, write_stream):
            logger.info("stdio_client connected, initializing session...")
            async with ClientSession(read_stream, write_stream) as session:
                self.session = session
                logger.info("Calling session.initialize()...")
                await session.initialize()
                logger.info("Session initialized.")

                tools_result = await session.list_tools()
                self.gemini_tool = mcp_tools_to_gemini(tools_result.tools)
                tool_names = [d.name for d in self.gemini_tool.function_declarations]
                logger.info("Connected. LLM tools: %s", tool_names)

                await self._control_loop()

    async def _control_loop(self) -> None:
        """Core loop: observe -> send to LLM -> execute tool -> repeat."""
        client = _get_client()

        while not self.finished:
            # 1. Fetch a fresh observation
            observation_b64 = await self._get_observation()

            # 2. Build the user message with the observation
            user_parts = self._build_observation_parts(observation_b64)
            self.contents.append(types.Content(role="user", parts=user_parts))

            # 3. Trim history to manage tokens
            self.contents = trim_history(self.contents)

            # 4. Call Gemini
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[self.gemini_tool],
                temperature=1.0,
            )

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=self.contents,
                config=config,
            )

            # 5. Append model response to history
            self.contents.append(response.candidates[0].content)

            # 6. Process parts — log text, execute function calls
            function_response_parts = []
            for part in response.candidates[0].content.parts:
                if getattr(part, "text", None):
                    logger.info("Agent: %s", part.text)

                if getattr(part, "function_call", None):
                    fc = part.function_call
                    result_text = await self._execute_tool(fc.name, dict(fc.args))

                    function_response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"result": result_text},
                        )
                    )

            # 7. If there were function calls, send results back
            if function_response_parts:
                self.contents.append(
                    types.Content(role="user", parts=function_response_parts)
                )

    async def _get_observation(self) -> str:
        """Call get_observation on the MCP server and return the base64 string."""
        result = await self.session.call_tool("get_observation", {})
        text = result.content[0].text if result.content else ""
        logger.info("get_observation returned %d chars, isError=%s, first 100: %s",
                     len(text), getattr(result, 'isError', None), text[:100])
        return text

    def _build_observation_parts(self, observation_b64: str) -> list[types.Part]:
        """Build Gemini parts with the current observation image."""
        parts: list[types.Part] = []

        # First message gets the mission
        if not self.contents:
            parts.append(types.Part(text=f"Your mission: {self.mission}"))

        parts.append(types.Part(text="Here is your current camera view:"))

        if observation_b64:
            image_bytes = base64.b64decode(observation_b64)
            # Detect mime type from magic bytes
            if image_bytes[:4] == b'\x89PNG':
                mime_type = "image/png"
            else:
                mime_type = "image/jpeg"
            logger.info("Image size: %d bytes, type: %s", len(image_bytes), mime_type)
            parts.append(types.Part(
                inline_data=types.Blob(
                    mime_type=mime_type,
                    data=image_bytes,
                )
            ))
        else:
            parts.append(types.Part(text="[no image available yet]"))

        parts.append(types.Part(text="What do you see? Decide your next action."))
        return parts

    async def _execute_tool(self, name: str, arguments: dict) -> str:
        """Execute an MCP tool and return the result text."""
        logger.info("Executing: %s(%s)", name, arguments)

        result = await self.session.call_tool(name, arguments)
        text = result.content[0].text if result.content else "(no output)"

        if name == "finish_mission":
            self.finished = True

        return text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="Robot Autonomous Agent")
    parser.add_argument("--mission", required=True, help="Mission objective")
    parser.add_argument(
        "--server-script",
        default="robot_server.py",
        help="Path to the MCP server script (default: robot_server.py)",
    )
    parser.add_argument("--depth-url", default="", help="Modal Depth Pro API URL (passed to server)")
    parser.add_argument("--port", default="", help="Teensy serial port (passed to server)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    import sys
    server_cmd = [sys.executable, args.server_script]
    if args.depth_url:
        server_cmd += ["--depth-url", args.depth_url]
    if args.port:
        server_cmd += ["--port", args.port]

    logger.info("Launching server: %s", server_cmd)
    controller = RobotController(mission=args.mission)
    await controller.run(server_cmd=server_cmd)
    logger.info("Agent finished.")


if __name__ == "__main__":
    asyncio.run(main())
