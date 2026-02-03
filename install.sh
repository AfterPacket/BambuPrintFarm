#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
