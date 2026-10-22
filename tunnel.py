import subprocess, re, os, requests, base64

token = os.environ["GH_TOKEN"]
repo = os.environ["GITHUB_REPOSITORY"]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

print("Starting tunnel...")
p = subprocess.Popen(["./cloudflared", "tunnel", "--url", "http://localhost:8000"], stderr=subprocess.PIPE, text=True)

url = None
for line in iter(p.stderr.readline, ''):
    match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
    if match:
        url = match.group(0)
        print(f"Tunnel URL found: {url}")
        break

if url:
    # ذخیره لینک در فایل proxy_url.txt
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/proxy_url.txt", headers=headers)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": "Update URL", "content": base64.b64encode(url.encode()).decode(), "branch": "main"}
    if sha: payload["sha"] = sha
    requests.put(f"https://api.github.com/repos/{repo}/contents/proxy_url.txt", json=payload, headers=headers)
    print("URL saved to repository.")

# زنده نگه داشتن اسکریپت تا زمانی که تونل باز است
p.wait()
