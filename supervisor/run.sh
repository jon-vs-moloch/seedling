#!/bin/bash
echo "Setting up ZenCode Supervisor..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
echo "Starting Supervisor Dashboard..."
uvicorn main:app --host 127.0.0.1 --port 7000
