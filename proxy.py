from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx, uvicorn

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
GOOGLE_API_BASE = "https://generativelanguage.googleapis.com"

@app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy(path: str, request: Request):
    if request.method == "OPTIONS":
        return Response(headers={"Access-Control-Allow-Origin": "*"})
    
    # دریافت کلید از هدر
    api_key = request.headers.get("x-goog-api-key")
    if not api_key:
        # اگر در هدر نبود، از Authorization بگیر
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            api_key = auth.split(" ")[1]
    
    if not api_key:
        return Response(content='{"error": "API Key missing"}', status_code=401)

    # ⭐ تغییر مهم: اضافه کردن کلید به عنوان پارامتر ?key= در URL
    target_url = f"{GOOGLE_API_BASE}/{path}?key={api_key}"
    
    body = await request.body()
    # هدر x-goog-api-key را دیگر نمی‌فرستیم چون کلید در URL هست
    headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(target_url, content=body, headers=headers)
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except Exception as e:
            return Response(content=str(e), status_code=500)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
