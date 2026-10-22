# proxy.py
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import uvicorn

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GOOGLE_API_URL = "https://generativelanguage.googleapis.com"

@app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy(path: str, request: Request):
    if request.method == "OPTIONS":
        return Response(headers={"Access-Control-Allow-Origin": "*"})

    api_key = request.headers.get("x-goog-api-key")
    if not api_key:
        return Response(content='{"error": "API Key missing"}', status_code=401)

    target_url = f"{GOOGLE_API_URL}/{path}"
    body = await request.body()
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(target_url, content=body, headers=headers)
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except Exception as e:
            return Response(content=str(e), status_code=500)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
