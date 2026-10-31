"""Six-hour GitHub Actions worker: legacy Gemini proxy + local Qwen decision queue."""
import os
import time
import requests
import json
import base64
import threading
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("GH_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
BRANCH = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"
if not TOKEN or not REPO:
    raise RuntimeError("GH_TOKEN and GITHUB_REPOSITORY must be set by GitHub Actions.")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "TLP-AI-Worker",
}

MODEL_REPO = "Qwen/Qwen3-4B-GGUF"
MODEL_FILE_NAME = "Qwen3-4B-Q4_K_M.gguf"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{MODEL_FILE_NAME}"
MODEL_DIR = Path(os.path.expanduser(os.environ.get("TLP_MODEL_CACHE", str(Path.home() / ".cache" / "tlp-ai"))))
MODEL_FILE = MODEL_DIR / MODEL_FILE_NAME
MODEL_CTX = 4096
MODEL_THREADS = max(1, min(4, os.cpu_count() or 2))
_MODEL = None
_MODEL_LOCK = threading.Lock()
_GENERATION_LOCK = threading.Lock()

print(f"TLP worker started for {REPO}@{BRANCH}.", flush=True)


def api_get(path):
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/contents/{path}",
            params={"ref": BRANCH}, headers=HEADERS, timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return base64.b64decode(data.get("content", "")).decode("utf-8"), data.get("sha")
        if r.status_code not in (404, 422):
            print(f"GitHub GET {path}: HTTP {r.status_code}", flush=True)
    except Exception as exc:
        print(f"GitHub GET error for {path}: {type(exc).__name__}", flush=True)
    return None, None


def api_list(path):
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/contents/{path}",
            params={"ref": BRANCH}, headers=HEADERS, timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
        if r.status_code not in (404, 422):
            print(f"GitHub LIST {path}: HTTP {r.status_code}", flush=True)
    except Exception as exc:
        print(f"GitHub LIST error for {path}: {type(exc).__name__}", flush=True)
    return []


def api_put(path, content):
    try:
        _, sha = api_get(path)
        payload = {
            "message": f"TLP worker update: {path}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": BRANCH,
        }
        if sha:
            payload["sha"] = sha
        r = requests.put(
            f"https://api.github.com/repos/{REPO}/contents/{path}",
            json=payload, headers=HEADERS, timeout=20,
        )
        if r.status_code not in (200, 201):
            print(f"GitHub PUT {path}: HTTP {r.status_code} {r.text[:160]}", flush=True)
            return False
        return True
    except Exception as exc:
        print(f"GitHub PUT error for {path}: {type(exc).__name__}", flush=True)
        return False


def api_delete(path):
    try:
        _, sha = api_get(path)
        if sha:
            payload = {"message": f"TLP queue cleanup: {path}", "sha": sha, "branch": BRANCH}
            requests.delete(
                f"https://api.github.com/repos/{REPO}/contents/{path}",
                json=payload, headers=HEADERS, timeout=15,
            )
    except Exception:
        pass


def _download_model():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if MODEL_FILE.exists() and MODEL_FILE.stat().st_size > 800 * 1024 * 1024:
        return True
    tmp = MODEL_FILE.with_suffix(MODEL_FILE.suffix + ".part")
    try:
        with requests.get(MODEL_URL, stream=True, timeout=(20, 90)) as response:
            response.raise_for_status()
            with open(tmp, "wb") as out:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
        if tmp.stat().st_size < 800 * 1024 * 1024:
            raise RuntimeError(f"Model download too small: {tmp.stat().st_size} bytes")
        os.replace(tmp, MODEL_FILE)
        print(f"Local decision model downloaded ({MODEL_FILE.stat().st_size // (1024 * 1024)} MB).", flush=True)
        return True
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"Decision model download unavailable: {type(exc).__name__}", flush=True)
        return False


def load_decision_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from llama_cpp import Llama
        except Exception as exc:
            print(f"Local decision runtime unavailable: {type(exc).__name__}; Gemini proxy remains active.", flush=True)
            return None
        if not MODEL_FILE.exists() and not _download_model():
            return None
        try:
            print(f"Loading {MODEL_FILE_NAME} (threads={MODEL_THREADS}, ctx={MODEL_CTX})...", flush=True)
            _MODEL = Llama(
                model_path=str(MODEL_FILE),
                n_ctx=MODEL_CTX,
                n_threads=MODEL_THREADS,
                n_batch=256,
                use_mmap=True,
                use_mlock=False,
                verbose=False,
            )
            print("Local decision model is ready.", flush=True)
            return _MODEL
        except Exception as exc:
            print(f"Decision model load failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            return None


def _extract_json(raw):
    raw = str(raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def run_local_decision(request_data):
    llm = load_decision_model()
    if llm is None:
        return {"error": "decision_model_unavailable", "engine": MODEL_FILE_NAME}
    prompt = str(request_data.get("prompt") or "").strip()[:6000]
    if not prompt:
        return {"error": "empty_prompt", "engine": MODEL_FILE_NAME}
    system = str(request_data.get("system") or "").strip()[:3000]
    system = (system + "\n\n" if system else "") + (
        "You are a precise decision engine. Analyze the request carefully. "
        "Return ONLY one valid JSON object in Persian unless another language is requested. "
        "Prefer these fields when relevant: decision, rationale, confidence (0..1), next_steps. "
        "Do not invent facts; state uncertainty explicitly."
    )
    try:
        max_tokens = max(64, min(900, int(request_data.get("max_tokens", 420))))
    except (TypeError, ValueError):
        max_tokens = 420
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    with _GENERATION_LOCK:
        try:
            try:
                result = llm.create_chat_completion(
                    messages=messages, temperature=0.1, top_p=0.85,
                    max_tokens=max_tokens, response_format={"type": "json_object"},
                )
            except (TypeError, ValueError):
                result = llm.create_chat_completion(
                    messages=messages, temperature=0.1, top_p=0.85,
                    max_tokens=max_tokens,
                )
            raw = result["choices"][0]["message"].get("content") or ""
        except Exception as exc:
            print(f"Decision generation error: {type(exc).__name__}", flush=True)
            return {"error": "decision_generation_failed", "engine": MODEL_FILE_NAME}
    parsed = _extract_json(raw)
    if parsed is None:
        return {"result": {"decision": raw[:5000], "confidence": 0.0, "parse_warning": True}, "engine": MODEL_FILE_NAME}
    return {"result": parsed, "engine": MODEL_FILE_NAME}


def process_proxy_request(request_data):
    try:
        key = base64.b64decode(request_data["api_key"]).decode("utf-8")
        model = request_data.get("model", "gemini-2.5-flash-lite")
        prompt = str(request_data.get("prompt") or "")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        response = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=40)
        if response.status_code == 200:
            reply = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            return {"reply": reply, "engine": model}
        return {"reply": f"Error {response.status_code}: {response.text[:500]}", "engine": model}
    except Exception as exc:
        return {"reply": f"Error 500: {type(exc).__name__}", "engine": "gemini-proxy"}


def cleanup_stale_queue(max_age_minutes=15):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    for folder in ("responses", "requests", "decision_responses", "decision_requests"):
        for item in api_list(folder):
            name = item.get("name", "")
            if not name.endswith(".json"):
                continue
            # The GitHub Contents API does not include created_at/updated_at in
            # directory listings. Read our own queue timestamp instead.
            raw, _ = api_get(folder + "/" + name)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                stamp = payload.get("created_at")
                if not stamp:
                    continue
                if isinstance(stamp, (int, float)):
                    created = datetime.fromtimestamp(float(stamp), tz=timezone.utc)
                else:
                    created = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if created < cutoff:
                    api_delete(folder + "/" + name)
            except Exception:
                continue


def _warm_model():
    try:
        load_decision_model()
    except Exception as exc:
        print(f"Decision model warmup skipped: {type(exc).__name__}; proxy remains available.", flush=True)


_DECISION_INFLIGHT = set()
_DECISION_INFLIGHT_LOCK = threading.Lock()


def _process_decision_async(msg_id, request_data):
    """Keep model loading/inference away from the Gemini proxy polling loop."""
    try:
        response_data = run_local_decision(request_data)
        response_data.setdefault("created_at", time.time())
        response_path = f"decision_responses/{msg_id}.json"
        if api_put(response_path, json.dumps(response_data, ensure_ascii=False)):
            print(f"Local decision response saved: {msg_id}", flush=True)
            api_delete(f"decision_requests/{msg_id}.json")
    except Exception as exc:
        print(f"Local decision {msg_id} failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    finally:
        with _DECISION_INFLIGHT_LOCK:
            _DECISION_INFLIGHT.discard(msg_id)


# Warm the local model in parallel so proxy requests continue while the model loads.
threading.Thread(target=_warm_model, name="tlp-decision-warmup", daemon=True).start()
print("Proxy + local decision queue are running.", flush=True)
_last_cleanup = 0

while True:
    try:
        if time.time() - _last_cleanup >= 300:
            cleanup_stale_queue()
            _last_cleanup = time.time()
        for queue_folder in ("requests", "decision_requests"):
            for file in api_list(queue_folder):
                name = file.get("name", "")
                if not name.endswith(".json"):
                    continue
                msg_id = name[:-5]
                queue_path = f"{queue_folder}/{msg_id}.json"
                content, _ = api_get(queue_path)
                if not content:
                    continue
                try:
                    request_data = json.loads(content)
                    if queue_folder == "decision_requests" or request_data.get("kind") == "decision":
                        # Model initialization and generation run separately; an AI decision
                        # cannot hold up regular Gemini proxy requests in this loop.
                        with _DECISION_INFLIGHT_LOCK:
                            if msg_id in _DECISION_INFLIGHT:
                                continue
                            _DECISION_INFLIGHT.add(msg_id)
                        try:
                            threading.Thread(
                                target=_process_decision_async,
                                args=(msg_id, request_data),
                                name=f"tlp-decision-{msg_id[:8]}",
                                daemon=True,
                            ).start()
                        except Exception:
                            with _DECISION_INFLIGHT_LOCK:
                                _DECISION_INFLIGHT.discard(msg_id)
                            raise
                    else:
                        print(f"Processing proxy request: {msg_id}", flush=True)
                        response_data = process_proxy_request(request_data)
                        response_data.setdefault("created_at", time.time())
                        if api_put(f"responses/{msg_id}.json", json.dumps(response_data, ensure_ascii=False)):
                            print(f"Response saved: {msg_id}", flush=True)
                            api_delete(f"requests/{msg_id}.json")
                except Exception as exc:
                    print(f"Request {msg_id} failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    except Exception as exc:
        print(f"Worker loop error: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    time.sleep(2)
