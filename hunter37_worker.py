#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Isolated GitHub Actions worker for Hunter37. Does not import or modify the TLP worker."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import subprocess
from pathlib import Path
from typing import Any

import requests

JEV_API = "https://api.jevstyle.com/v1/systemone"
REQUEST_DIR = Path("hunter37/requests")
RESPONSE_DIR = Path("hunter37/responses")


def get_jev_key() -> str:
    """Read the secret from GitHub Actions environment; never from a public queue file."""
    return os.getenv("HUNTER37_JEV_API_KEY", "").strip()


def call_jev(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "Hunter37-GitHub-Worker/1.0"}
    for attempt in range(4):
        try:
            response = requests.post(JEV_API, json=payload, headers=headers, timeout=(12, 75))
        except requests.RequestException as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error_code": f"NETWORK_{type(exc).__name__}"}
        if response.status_code in (200, 201):
            try:
                data = response.json()
            except ValueError:
                return {"ok": False, "error_code": "JEV_INVALID_JSON"}
            if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                return {"ok": False, "error_code": "JEV_INVALID_RESPONSE"}
            return {"ok": True, "provider": "Jev-Style", "result": data}
        if response.status_code == 429 or response.status_code in (500, 502, 503, 504, 529):
            retry = response.headers.get("Retry-After", "")
            try:
                delay = max(1, min(45, int(float(retry)))) if retry else 2 ** attempt
            except ValueError:
                delay = 2 ** attempt
            if attempt < 3:
                time.sleep(delay)
                continue
        return {"ok": False, "error_code": f"JEV_HTTP_{response.status_code}"}
    return {"ok": False, "error_code": "JEV_RETRIES_EXHAUSTED"}


def process_one(path: Path) -> bool:
    match = re.fullmatch(r"([A-Za-z0-9_-]{8,80})\.json", path.name)
    if not match:
        logging.warning("Skipping invalid request filename")
        return False
    task_id = match.group(1)
    response_path = RESPONSE_DIR / f"{task_id}.json"
    if response_path.exists():
        path.unlink(missing_ok=True)
        return True
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result = {"ok": False, "error_code": "INVALID_REQUEST_JSON"}
        request = {}
    else:
        key = get_jev_key()
        payload = request.get("payload")
        if request.get("kind") != "jev_decision":
            result = {"ok": False, "error_code": "INVALID_TASK_KIND"}
        elif not key:
            result = {"ok": False, "error_code": "MISSING_HUNTER37_JEV_ACTION_SECRET"}
        elif not isinstance(payload, dict) or not isinstance(payload.get("state"), (str, dict, list)) or not isinstance(payload.get("questions"), dict):
            result = {"ok": False, "error_code": "INVALID_JEV_PAYLOAD"}
        else:
            result = call_jev(key, payload)
    result["request_id"] = task_id
    result["completed_at"] = int(time.time())
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    path.unlink(missing_ok=True)
    logging.info("Hunter37 request %s processed: %s", task_id[:8], "ok" if result.get("ok") else result.get("error_code", "error"))
    return True


def main() -> int:
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    RESPONSE_DIR.mkdir(parents=True, exist_ok=True)
    items = sorted(REQUEST_DIR.glob("*.json"))
    if not items:
        logging.info("No Hunter37 requests in queue")
        return 0
    processed = 0
    for path in items[:20]:
        try:
            processed += int(process_one(path))
        except Exception as exc:
            logging.exception("Failed processing %s (%s)", path.name, type(exc).__name__)
    if processed:
        try:
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
            subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
            subprocess.run(["git", "add", "hunter37/requests", "hunter37/responses"], check=True)
            changed = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode != 0
            if changed:
                subprocess.run(["git", "commit", "-m", "Hunter37: process Jev decision queue"], check=True)
                branch = os.getenv("GITHUB_REF_NAME", "main")
                # A new Termux request can be committed while this action is running.
                # Rebase the worker result on that newer commit before pushing.
                subprocess.run(["git", "pull", "--rebase", "origin", branch], check=True, timeout=90)
                subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], check=True, timeout=90)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logging.error("Could not commit/push Hunter37 response files (%s)", type(exc).__name__)
            return 2
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
