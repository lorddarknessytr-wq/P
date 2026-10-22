import os, time, requests, json, base64

token = os.environ["GH_TOKEN"]
repo = os.environ["GITHUB_REPOSITORY"]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

def api_get(path):
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers)
    if r.status_code == 200:
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8").strip(), data["sha"]
    return None, None

def api_list(path):
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers)
    return r.json() if r.status_code == 200 else []

def api_put(path, content):
    _, sha = api_get(path)
    payload = {"message": f"Update {path}", "content": base64.b64encode(content.encode()).decode(), "branch": "main"}
    if sha: payload["sha"] = sha
    requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", json=payload, headers=headers)

def api_delete(path):
    _, sha = api_get(path)
    if sha:
        payload = {"message": f"Delete {path}", "sha": sha, "branch": "main"}
        requests.delete(f"https://api.github.com/repos/{repo}/contents/{path}", json=payload, headers=headers)

print("Worker started. Waiting for requests...")
while True:
    reqs = api_list("requests")
    if isinstance(reqs, list):
        for file in reqs:
            if file["name"].endswith(".json"):
                msg_id = file["name"].replace(".json", "")
                res, _ = api_get(f"responses/{msg_id}.json")
                if not res: # اگر هنوز جواب داده نشده
                    content, _ = api_get(f"requests/{msg_id}.json")
                    if content:
                        try:
                            req = json.loads(content)
                            key = req["api_key"]
                            model = req.get("model", "gemini-3.8-flash")
                            prompt = req["prompt"]
                            
                            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                            payload = {"contents": [{"parts": [{"text": prompt}]}]}
                            
                            r = requests.post(url, json=payload, timeout=30)
                            if r.status_code == 200:
                                reply = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                            else:
                                reply = f"Error {r.status_code}: {r.text}"
                            
                            api_put(f"responses/{msg_id}.json", json.dumps({"reply": reply}))
                            api_delete(f"requests/{msg_id}.json")
                        except Exception as e:
                            print("Error:", e)
    time.sleep(3)
