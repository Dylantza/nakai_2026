#!/bin/bash

# Load environment variables from .env file
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

cd /Users/dylantzachar/Desktop/Projects/eng_test

./venv/bin/python mcp_agent/web_app.py --depth-url "https://dylantza--depth-pro-api-depthmodel-predict.modal.run"
