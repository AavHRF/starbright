#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -f .env ]; then
    echo "Missing .env (copy .env.example and fill in your values)" >&2
    exit 1
fi

exec uv run python run.py
