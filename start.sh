#!/usr/bin/env bash
set -euo pipefail

python -m pip install --disable-pip-version-check -r requirements.txt

if [[ -z "${GIST_TOKEN:-}" || -z "${GIST_ID:-}" ]]; then
  echo "GIST_TOKEN and GIST_ID are required for the optional Cloudflare URL publisher." >&2
  exit 1
fi

CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
wget -q "$CLOUDFLARED_URL" -O cloudflared
chmod +x cloudflared

python -u proxy.py &
PROXY_PID=$!
cleanup() {
  kill "$PROXY_PID" 2>/dev/null || true
  if [[ -n "${TUNNEL_PID:-}" ]]; then kill "$TUNNEL_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

sleep 3
./cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 > /tmp/tunnel.log 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 45); do
  URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/tunnel.log | head -n 1 || true)"
  [[ -n "$URL" ]] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || { echo "cloudflared exited unexpectedly" >&2; exit 1; }
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "Failed to obtain a Cloudflare Quick Tunnel URL" >&2
  cat /tmp/tunnel.log >&2
  exit 1
fi

echo "Cloudflare URL is ready."
python - "$URL" <<'PY'
import json, os, sys, urllib.request
url = sys.argv[1]
gist_id = os.environ["GIST_ID"]
token = os.environ["GIST_TOKEN"]
body = json.dumps({"files": {"proxy_url.txt": {"content": url}}}).encode()
request = urllib.request.Request(
    f"https://api.github.com/gists/{gist_id}",
    data=body,
    method="PATCH",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "TLP-Proxy-URL-Publisher",
    },
)
with urllib.request.urlopen(request, timeout=20) as response:
    if response.status not in (200, 201):
        raise SystemExit(f"Could not update URL gist: HTTP {response.status}")
print("Proxy URL published successfully.")
PY

wait "$PROXY_PID" "$TUNNEL_PID"
