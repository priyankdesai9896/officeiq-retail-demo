#!/usr/bin/env bash
# Start the app on http://localhost:8502 (override with APP_PORT).
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/streamlit run app.py --server.port "${APP_PORT:-8502}" --server.headless true "$@"
