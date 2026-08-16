#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

bash scripts/smoke_environment.sh --preflight

cat <<'EOF'

Lab 8 environment ready.
Run the full container and Prometheus check before beginning the lab:
  bash scripts/smoke_environment.sh
EOF
