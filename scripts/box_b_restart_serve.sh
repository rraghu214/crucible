#!/usr/bin/env bash
# Box B: restart Crucible's UI server, and keep the public HTTPS tunnel up.
#
# Installed as ~/restart-serve.sh:
#   cp scripts/box_b_restart_serve.sh ~/restart-serve.sh && chmod +x ~/restart-serve.sh
#
# Safe to run over SSH: it stops the server by matching the venv binary path,
# which never appears in the calling command line. (A bare
# `pkill -f "crucible serve"` also matches the SSH session that runs it, and
# kills it before the server comes back.)
#
# The tunnel is a Cloudflare quick tunnel (DESIGN.md 18): HTTPS ends at
# Cloudflare, the tunnel is opened outward from this box, and no inbound port
# opens. `cloudflared` must be installed. Remove the tunnel block after
# submission.
cd /home/ubuntu/crucible || exit 1
pkill -f "/.venv/bin/crucible serve"
sleep 2
setsid nohup /home/ubuntu/.local/bin/uv run crucible serve >> /home/ubuntu/crucible-serve.log 2>&1 < /dev/null &
sleep 10
curl -s 127.0.0.1:8113/healthz
echo

# Public HTTPS tunnel (until submission). Started only when the site login is
# configured, so the dashboard is never published without it. Left running
# across server restarts, because restarting it changes the public URL.
if grep -q '^CRUCIBLE_UI_USER=.\+' .env && grep -q '^CRUCIBLE_UI_PASSWORD=.\+' .env; then
  if ! pgrep -f "cloudflared tunnel --url" > /dev/null; then
    setsid nohup cloudflared tunnel --url http://127.0.0.1:8113 > /home/ubuntu/cloudflared.log 2>&1 < /dev/null &
    sleep 10
  fi
  echo "public URL: $(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /home/ubuntu/cloudflared.log | tail -1)/dashboard"
else
  echo "tunnel NOT started: set CRUCIBLE_UI_USER and CRUCIBLE_UI_PASSWORD in ~/crucible/.env first"
fi
