#!/bin/bash
echo "Setting up Zen Agent OS..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
echo "Starting Zen Server..."
uvicorn app:app --host 127.0.0.1 --port 8000
