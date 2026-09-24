#!/bin/bash
# Wrapper that launches the long-lived Chrome instance under supervisord.
# Kept separate from supervisord.conf because the flag list is long and
# would be unreadable inline.
#
# This is the SAME Chrome serving both the Desktop tab (rendered via
# Xvfb → x11vnc → noVNC) and the Browser tab (CDP on :9222).  exec'd so
# supervisord supervises chromium directly (no wrapping shell PID in
# the way of stopasgroup signal propagation).

set -e

PROFILE_DIR=/root/.browser_profiles/default

# Behind the egress gate (docker-compose.egress.yml) the container has no route
# out except the proxy, so Chrome must use it explicitly.
# Behind the gate every connection is a policy decision, so Chrome's own
# background traffic (component updates, time checks, safe-browsing, sync)
# is switched off rather than turned into a stream of questions for a person.
# (Loopback stays bypassed — Chrome's default — so a dev server the agent
# starts inside the sandbox still opens in the browser.)
DISABLED_FEATURES="BlockThirdPartyCookies"
PROXY_ARGS=()
if [ -n "${HTTPS_PROXY:-}" ]; then
  PROXY_ARGS=(
    --proxy-server="$HTTPS_PROXY"
    --disable-background-networking --disable-component-update
    --disable-sync --disable-domain-reliability --no-pings
  )
  # Chrome honours only the LAST --disable-features, so extend the one list.
  DISABLED_FEATURES="$DISABLED_FEATURES,OptimizationHints,MediaRouter"
fi

exec /usr/bin/chromium-browser \
  "${PROXY_ARGS[@]}" \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --disable-blink-features=AutomationControlled \
  --disable-popup-blocking \
  --disable-features="$DISABLED_FEATURES" \
  --load-extension=/opt/prax-cast-ext \
  --remote-allow-origins=http://127.0.0.1:9222 \
  --user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE_DIR" \
  --window-size=1920,1080
