#!/bin/bash
echo "Setting up Operator Dashboard..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
echo "Starting Operator API..."
uvicorn main:app --host 127.0.0.1 --port 9000
