#!/usr/bin/env python3
"""Optional direct Gemini HTTP proxy (legacy Cloudflare-tunnel mode).

The TLP bot's current GitHub integration uses worker.py, not this server.
This server is kept for deployments that explicitly use the Cloudflare URL.
"""

import os
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="TLP Gemini HTTP Proxy")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

GOOGLE_API_BASE = "https://generativelanguage.googleapis.com"
ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@app.api_route("/{path:path}", methods=ALLOWED_METHODS)
async def proxy(path: str, request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204)

    access_token = os.getenv("PROXY_ACCESS_TOKEN", "").strip()
    if access_token and request.headers.get("x-proxy-token", "") != access_token:
        return Response(content='{"error":"proxy authentication required"}', status_code=403,
                        media_type="application/json")

    api_key = request.headers.get("x-goog-api-key", "").strip()
    if not api_key:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            api_key = authorization[7:].strip()
    if not api_key:
        api_key = request.query_params.get("key", "").strip()
    if not api_key:
        return Response(content='{"error":"API key missing"}', status_code=401,
                        media_type="application/json")

    safe_path = "/".join(quote(part, safe=":@._-") for part in path.split("/"))
    target_url = f"{GOOGLE_API_BASE}/{safe_path}"
    query = [(key, value) for key, value in request.query_params.multi_items() if key != "key"]
    body = await request.body()

    headers = {"x-goog-api-key": api_key}
    content_type = request.headers.get("content-type")
    accept = request.headers.get("accept")
    if content_type:
        headers["content-type"] = content_type
    if accept:
        headers["accept"] = accept

    timeout = httpx.Timeout(90.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                target_url,
                params=query,
                content=body if body else None,
                headers=headers,
            )
        response_headers = {}
        for header_name in ("retry-after", "x-goog-request-id"):
            if header_name in upstream.headers:
                response_headers[header_name] = upstream.headers[header_name]
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers=response_headers,
        )
    except httpx.TimeoutException:
        return Response(content='{"error":"upstream timeout"}', status_code=504,
                        media_type="application/json")
    except httpx.HTTPError:
        return Response(content='{"error":"upstream connection failed"}', status_code=502,
                        media_type="application/json")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
