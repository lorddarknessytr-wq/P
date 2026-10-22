#!/bin/bash
pip install fastapi uvicorn httpx

# دانلود cloudflared
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
chmod +x cloudflared

# اجرای سرور در پس‌زمینه
python proxy.py &
sleep 5

# ساخت تونل (این دستور تا زمانی که جاب فعال است، زنده می‌ماند)
./cloudflared tunnel --url http://localhost:8000
