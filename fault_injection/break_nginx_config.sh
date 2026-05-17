#!/usr/bin/env sh
set -eu

COMPOSE="${COMPOSE:-docker compose}"

$COMPOSE exec -T agent sh -c "printf '%s\n' 'server {' '    listen 80;' '    definitely_invalid_directive on;' '}' > /nginx_conf/site.conf"

# Restarting applies the invalid conf.d snippet. A non-zero restart is expected
# when nginx refuses to start with the injected configuration.
$COMPOSE restart nginx >/dev/null 2>&1 || true

echo "Injected invalid nginx conf.d/site.conf. Nginx should now fail config load."

