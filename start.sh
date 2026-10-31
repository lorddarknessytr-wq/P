#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$ROOT_DIR/cloudflared}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/tlp-cloudflared.log}"

if [[ ! -x "$CLOUDFLARED_BIN" ]]; then
  wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O "$CLOUDFLARED_BIN"
  chmod +x "$CLOUDFLARED_BIN"
fi

python -u proxy.py &
PROXY_PID=$!
python -u worker.py &
WORKER_PID=$!
"$CLOUDFLARED_BIN" tunnel --no-autoupdate --url http://127.0.0.1:8000 >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

cleanup() {
  for pid in "$PROXY_PID" "$WORKER_PID" "$TUNNEL_PID"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n 1 || true)"
  [[ -n "$URL" ]] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || break
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "Cloudflare tunnel did not provide a public URL." >&2
  tail -n 40 "$TUNNEL_LOG" || true
  exit 1
fi

echo "TLP Gemini proxy URL: $URL"
echo "TLP decision worker is running independently beside the proxy."

# Persist the latest tunnel URL in the repository, using the workflow token.
if [[ -n "${GH_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" ]]; then
  PROXY_URL="$URL" python - <<'PY'
import os, base64, requests
repo = os.environ["GITHUB_REPOSITORY"]
branch = os.environ.get("GITHUB_REF_NAME", "main") or "main"
token = os.environ["GH_TOKEN"]
url = os.environ["PROXY_URL"]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
api = f"https://api.github.com/repos/{repo}/contents/proxy_url.txt"
try:
    old = requests.get(api, params={"ref": branch}, headers=headers, timeout=15)
    payload = {"message": "Update TLP proxy URL", "content": base64.b64encode(url.encode()).decode(), "branch": branch}
    if old.status_code == 200:
        payload["sha"] = old.json().get("sha")
    elif old.status_code not in (404, 422):
        old.raise_for_status()
    response = requests.put(api, json=payload, headers=headers, timeout=20)
    response.raise_for_status()
    print("Latest proxy URL saved to proxy_url.txt.", flush=True)
except Exception as exc:
    print(f"Could not save proxy_url.txt: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
PY
fi

# Keep the previous optional Gist publishing behavior for existing deployments.
if [[ -n "${GIST_TOKEN:-}" && -n "${GIST_ID:-}" ]]; then
  curl -fsS -X PATCH \
    -H "Authorization: Bearer $GIST_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    -d "{\"files\":{\"proxy_url.txt\":{\"content\":\"$URL\"}}}" \
    "https://api.github.com/gists/$GIST_ID" >/dev/null || echo "Gist URL update failed; proxy and decision worker continue." >&2
fi

# If any of the three long-running processes exits, end this job cleanly.
while true; do
  for pair in "proxy:$PROXY_PID" "decision-worker:$WORKER_PID" "cloudflared:$TUNNEL_PID"; do
    name="${pair%%:*}"
    pid="${pair#*:}"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$name stopped; ending combined proxy/decision job." >&2
      exit 1
    fi
  done
  sleep 5
done
