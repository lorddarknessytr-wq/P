import os, time, requests, json, base64

token = os.environ["GH_TOKEN"]
repo = os.environ["GITHUB_REPOSITORY"]
headers = {
    "Authorization": f"Bearer {token}", 
    "Accept": "application/vnd.github+json",
    "User-Agent": "GitHub-Action-Worker"
}

print("🚀 Worker script started.", flush=True)

def api_get(path):
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return base64.b64decode(data["content"]).decode("utf-8").strip(), data["sha"]
    except Exception as e:
        print(f"API GET error for {path}: {e}", flush=True)
    return None, None

def api_list(path):
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            print(f"⚠️ Folder {path} not found. Please create it.", flush=True)
            return []
        print(f"Error listing {path}: {r.status_code}", flush=True)
    except Exception as e:
        print(f"API LIST error for {path}: {e}", flush=True)
    return []

def api_put(path, content):
    try:
        _, sha = api_get(path)
        payload = {"message": f"Update {path}", "content": base64.b64encode(content.encode()).decode(), "branch": "main"}
        if sha: payload["sha"] = sha
        r = requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", json=payload, headers=headers, timeout=15)
        if r.status_code not in (200, 201):
            print(f"Error putting {path}: {r.text}", flush=True)
    except Exception as e:
        print(f"API PUT error for {path}: {e}", flush=True)

def api_delete(path):
    try:
        _, sha = api_get(path)
        if sha:
            payload = {"message": f"Delete {path}", "sha": sha, "branch": "main"}
            requests.delete(f"https://api.github.com/repos/{repo}/contents/{path}", json=payload, headers=headers, timeout=15)
    except Exception as e:
        print(f"API DELETE error for {path}: {e}", flush=True)

print("🚀 Worker is running and waiting for requests...", flush=True)

while True:
    try:
        reqs = api_list("requests")
        for file in reqs:
            if file["name"].endswith(".json"):
                msg_id = file["name"].replace(".json", "")
                print(f"🔍 Found request: {msg_id}", flush=True)
                
                content, _ = api_get(f"requests/{msg_id}.json")
                if content:
                    try:
                        req = json.loads(content)
                        
                        # ⭐ رمزگشایی کلید API
                        b64_key = req["api_key"]
                        key = base64.b64decode(b64_key).decode('utf-8')
                        
                        model = req.get("model", "gemini-3.8-flash")
                        prompt = req["prompt"]
                        
                        print(f"📡 Sending to Gemini (Model: {model})...", flush=True)
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                        payload = {"contents": [{"parts": [{"text": prompt}]}]}
                        
                        r = requests.post(url, json=payload, timeout=30)
                        if r.status_code == 200:
                            reply = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                            print("✅ Gemini replied successfully.", flush=True)
                        else:
                            reply = f"Error {r.status_code}: {r.text}"
                            print(f"❌ Gemini error: {reply}", flush=True)
                        
                        api_put(f"responses/{msg_id}.json", json.dumps({"reply": reply}))
                        print(f"💾 Response saved for {msg_id}", flush=True)
                        
                        api_delete(f"requests/{msg_id}.json")
                    except Exception as e:
                        print(f"⚠️ Error processing {msg_id}: {e}", flush=True)
    except Exception as e:
        print(f"Global loop error: {e}", flush=True)
        
    time.sleep(3)
