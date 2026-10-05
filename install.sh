#!/usr/bin/env bash
# Sets up the venv and installs/starts the systemd user service.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
chmod 600 .env
mkdir -p ~/.config/systemd/user
ln -sf "$PWD/solar-dashboard.service" ~/.config/systemd/user/solar-dashboard.service
systemctl --user daemon-reload
systemctl --user enable --now solar-dashboard
echo "Running at http://127.0.0.1:8765 — edit .env, then: systemctl --user restart solar-dashboard"
