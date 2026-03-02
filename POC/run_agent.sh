#!/bin/bash
# Load environment variables from .env file
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi
cd /Users/dylantzachar/Desktop/Projects/eng_test
./venv/bin/python mcp_agent/robot_agent.py --mission "Go to the human" --server-script mcp_agent/robot_server.py --depth-url "https://dylantza--depth-pro-api-depthmodel-predict.modal.run"
