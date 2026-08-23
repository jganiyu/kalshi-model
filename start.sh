#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
KALSHI_MODEL_OPEN_BROWSER="${KALSHI_MODEL_OPEN_BROWSER:-1}" .venv/bin/python -m app
