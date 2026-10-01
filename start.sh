#!/usr/bin/env bash
# Start Minecraft Skill Builder (WSL, Linux or macOS).
# First run: installs uv (which brings its own Python) and the app's dependencies.
# Any arguments are passed on, e.g. ./start.sh --trigger '!ai'
set -euo pipefail
cd "$(dirname "$0")"

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python package manager) for this user..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Trust the system certificate store (school and company networks often inspect HTTPS).
export UV_SYSTEM_CERTS=1
exec uv run --frozen skillbuilder --open "$@"
