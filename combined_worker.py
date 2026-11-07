#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One GitHub Actions process for two strictly separate queues.

The existing worker.py is imported and used unchanged for Rubika's requests/ queue.
Hunter37 uses only hunter37/requests and hunter37/responses, with a separate Jev key.
Both queues share one serial polling loop and one GitHub API client, so they do not
race to commit repository changes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import requests
from worker import ProxyWorker

JEV_API = "https://api.jevstyle.com/v1/systemone"
HUNTER_REQUEST_DIR = "hunter37/requests"
HUNTER_RESPONSE_DIR = "hunter37/responses"


def _call_jev(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "Hunter37-Shared-Actions-Worker/1.0",
    }
    with requests.Session() as session:
        for attempt in range(4):
            try:
                response = session.post(JEV_API, json=payload, headers=headers, timeout=(12, 75))
            except requests.RequestException as exc:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                return {"ok": False, "provider": "Jev-Style", "error_code": f"NETWORK_{type(exc).__name__}"}

            if response.status_code in (200, 201):
                try:
                    data = response.json()
                except ValueError:
                    return {"ok": False, "provider": "Jev-Style", "error_code": "JEV_INVALID_JSON"}
                if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                    return {"ok": False, "provider": "Jev-Style", "error_code": "JEV_INVALID_RESPONSE"}
                return {"ok": True, "provider": "Jev-Style", "result": data}

            retryable = response.status_code == 429 or response.status_code in (500, 502, 503, 504, 529)
            if retryable and attempt < 3:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(1, min(45, int(float(retry_after)))) if retry_after else 2 ** attempt
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                time.sleep(delay)
                continue
            return {"ok": False, "provider": "Jev-Style", "error_code": f"JEV_HTTP_{response.status_code}"}

    return {"ok": False, "provider": "Jev-Style", "error_code": "JEV_RETRIES_EXHAUSTED"}


def _process_one_hunter(proxy: ProxyWorker, name: str, jev_key: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}\.json", name):
        logging.warning("Hunter37: ignoring invalid request filename")
        return False

    task_id = name[:-5]
    request_path = f"{HUNTER_REQUEST_DIR}/{name}"
    response_path = f"{HUNTER_RESPONSE_DIR}/{task_id}.json"

    # A prior response means the request was already processed; never call Jev twice.
    _old_response, _old_sha, old_status = proxy.get_text_file(response_path)
    if old_status == 200:
        proxy.delete_file(request_path)
        return True
    if old_status not in (0, 404):
        logging.warning("Hunter37: cannot inspect response slot %s (HTTP %s)", task_id[:8], old_status)
        return False
    if old_status == 0:
        return False

    raw, _request_sha, status = proxy.get_text_file(request_path)
    if status != 200 or raw is None:
        logging.warning("Hunter37: cannot read request %s (HTTP %s)", task_id[:8], status)
        return False

    try:
        request_data = json.loads(raw)
    except (ValueError, TypeError):
        result: dict[str, Any] = {"ok": False, "error_code": "INVALID_REQUEST_JSON"}
    else:
        payload = request_data.get("payload") if isinstance(request_data, dict) else None
        kind = request_data.get("kind") if isinstance(request_data, dict) else None
        if kind != "jev_decision":
            result = {"ok": False, "error_code": "INVALID_TASK_KIND"}
        elif not jev_key:
            result = {"ok": False, "error_code": "MISSING_HUNTER37_JEV_ACTION_SECRET"}
        elif not isinstance(payload, dict) or not isinstance(payload.get("state"), (str, dict, list)) or not isinstance(payload.get("questions"), dict):
            result = {"ok": False, "error_code": "INVALID_JEV_PAYLOAD"}
        else:
            result = _call_jev(jev_key, payload)

    result["request_id"] = task_id
    result["completed_at"] = int(time.time())
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if not proxy.put_text_file(response_path, encoded):
        logging.warning("Hunter37: could not save response %s; request retained for retry", task_id[:8])
        return False
    proxy.delete_file(request_path)
    logging.info("Hunter37: request %s processed (%s)", task_id[:8], "ok" if result.get("ok") else result.get("error_code", "error"))
    return True


def process_hunter_queue(proxy: ProxyWorker, max_batch: int = 3) -> int:
    jev_key = os.getenv("HUNTER37_JEV_API_KEY", "").strip()
    try:
        items = proxy.list_json_files(HUNTER_REQUEST_DIR)
    except Exception as exc:
        logging.warning("Hunter37: queue listing failed (%s)", type(exc).__name__)
        return 0

    processed = 0
    for item in items[:max_batch]:
        name = str(item.get("name", "")) if isinstance(item, dict) else ""
        if not name:
            continue
        try:
            processed += int(_process_one_hunter(proxy, name, jev_key))
        except Exception as exc:
            logging.exception("Hunter37: queue item failed (%s)", type(exc).__name__)
    return processed


def dispatch_next_run(repository: str, branch: str, token: str) -> bool:
    """Queue the next worker before this GitHub-hosted job reaches its 6-hour cap."""
    if os.getenv("SHARED_WORKER_AUTO_RESTART", "true").strip().lower() in {"0", "false", "no", "off"}:
        logging.info("Automatic worker restart is disabled by SHARED_WORKER_AUTO_RESTART")
        return False
    if not token or repository.count("/") != 1:
        logging.error("Cannot auto-restart: missing GH_TOKEN or repository")
        return False
    owner, repo = repository.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/proxy.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TLP-Hunter37-Shared-Worker/1.0",
    }
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json={"ref": branch}, timeout=(8, 20))
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            logging.error("Automatic workflow dispatch failed (%s)", type(exc).__name__)
            return False
        if response.status_code in (200, 201, 202, 204):
            logging.info("Next shared worker run queued successfully; no manual per-request run is needed")
            return True
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        logging.error("Automatic workflow dispatch rejected: HTTP %s; check Actions: write permission", response.status_code)
        return False
    return False


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    token = os.getenv("GH_TOKEN", "").strip()
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    branch = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"
    if not token or not repository:
        logging.error("Missing GH_TOKEN or GITHUB_REPOSITORY")
        return 2

    try:
        proxy = ProxyWorker(token=token, repository=repository, branch=branch)
    except Exception as exc:
        logging.error("Shared worker configuration failed (%s)", type(exc).__name__)
        return 2

    try:
        poll_seconds = max(1.0, min(15.0, float(os.getenv("POLL_SECONDS", "3"))))
    except ValueError:
        poll_seconds = 3.0
    try:
        max_runtime = max(600, min(21300, int(os.getenv("WORKER_MAX_RUNTIME_SECONDS", "21300"))))
    except ValueError:
        max_runtime = 21300

    started = time.monotonic()
    logging.info("Combined Actions worker started for %s (branch %s)", repository, branch)
    logging.info("Rubika queue: requests/ -> responses/ | Hunter37 queue: hunter37/requests/ -> hunter37/responses/")
    logging.info("One serial worker is polling both queues every %.1f seconds", poll_seconds)
    if not os.getenv("HUNTER37_JEV_API_KEY", "").strip():
        logging.error("HUNTER37_JEV_API_KEY is missing; Rubika remains active, Hunter37 replies will report a missing secret")

    while time.monotonic() - started < max_runtime:
        cycle_started = time.monotonic()
        try:
            rubika_count = proxy.process_pending_requests()
            if rubika_count:
                logging.info("Rubika queue: processed %s item(s)", rubika_count)
        except Exception as exc:
            logging.exception("Rubika queue cycle failed (%s)", type(exc).__name__)

        try:
            hunter_count = process_hunter_queue(proxy, max_batch=3)
            if hunter_count:
                logging.info("Hunter37 queue: processed %s item(s)", hunter_count)
        except Exception as exc:
            logging.exception("Hunter37 queue cycle failed (%s)", type(exc).__name__)

        remaining = poll_seconds - (time.monotonic() - cycle_started)
        if remaining > 0:
            time.sleep(remaining)

    logging.warning("Shared worker reached its planned runtime limit; queuing the next run automatically")
    dispatch_next_run(repository, branch, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
