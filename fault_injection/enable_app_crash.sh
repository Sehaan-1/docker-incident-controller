#!/usr/bin/env sh
set -eu

COMPOSE="${COMPOSE:-docker compose}"

$COMPOSE exec -T agent python -c "import json; from pathlib import Path; Path('/runtime/flags.json').write_text(json.dumps({'crash_on_start': True}) + '\n', encoding='utf-8')"

# The app reads runtime flags at startup, so restart is the controlled fault edge.
$COMPOSE restart app >/dev/null 2>&1 || true

echo "Enabled app crash_on_start flag and restarted app. The app should now crash on startup."
