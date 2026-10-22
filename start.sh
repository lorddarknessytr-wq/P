#!/bin/bash
set -e

wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
chmod +x cloudflared

python proxy.py &
PROXY_PID=$!
sleep 5

./cloudflared tunnel --url http://localhost:8000 > /tmp/tunnel.log 2>&1 &
TUNNEL_PID=$!

for i in {1..30}; do
  URL=$(grep -o 'https://[^ ]*\.trycloudflare\.com' /tmp/tunnel.log | head -1)
  if [ -n "$URL" ]; then break; fi
  sleep 1
done

if [ -z "$URL" ]; then
  echo "Failed to get tunnel URL"
  exit 1
fi

echo "Tunnel URL: $URL"

curl -s -X PATCH \
  -H "Authorization: Bearer $GIST_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d "{\"files\":{\"proxy_url.txt\":{\"content\":\"$URL\"}}}" \
  https://api.github.com/gists/$GIST_ID

wait $PROXY_PID $TUNNEL_PID
