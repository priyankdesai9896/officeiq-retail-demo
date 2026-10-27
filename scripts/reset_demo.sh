#!/usr/bin/env bash
# Clear the semantic cache and its token tally, remembered photo readings and the
# activity log. Restart the app afterwards so every session starts fresh.
cd "$(dirname "$0")/.." || exit 1
PYTHONPATH=. .venv/bin/python -c "
from retail import semantic_cache, vision, agentc_bridge
semantic_cache.reset(); vision.forget_photos(); agentc_bridge.clear_activity()
print('reset:', semantic_cache.totals())"
