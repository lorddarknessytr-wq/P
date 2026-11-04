#!/usr/bin/env python3
"""GitHub-Contents queue worker used by the TLP Runflare bot.

The bot writes requests/<uuid>.json and polls responses/<uuid>.json.
This worker runs inside GitHub Actions, calls Gemini from the Actions runner,
then writes the reply back to the same repository.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import sys
import time
from typing import Any, Optional
from urllib.parse import quote

import requests

GITHUB_API = "https://api.github.com"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
USER_AGENT = "TLP-GitHub-Proxy-Worker/2.0"


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def decode_api_key(encoded: Any) -> Optional[str]:
    """Decode the base64 key format currently written by TLP's ai_service.py."""
    if not isinstance(encoded, str) or not encoded.strip():
        return None
    try:
        key = base64.b64decode(encoded.strip(), validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return key or None


def extract_gemini_text(payload: Any) -> str:
    """Safely extract the text from a Gemini generateContent response."""
    if not isinstance(payload, dict):
        return ""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return ""
    pieces: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
    return "".join(pieces).strip()


class ProxyWorker:
    def __init__(
        self,
        token: str,
        repository: str,
        branch: str = "main",
        session: Optional[requests.Session] = None,
        provider_session: Optional[requests.Session] = None,
    ) -> None:
        if not token or not repository or "/" not in repository:
            raise ValueError("GH_TOKEN and GITHUB_REPOSITORY are required")

        self.token = token.strip()
        self.repository = repository.strip()
        self.branch = (branch or "main").strip()
        # Keep GitHub credentials on a dedicated session. Never reuse this
        # session for Gemini: a session-level Authorization header would leak
        # the GitHub token to Google's API.
        self.session = session or requests.Session()
        self.provider_session = provider_session or requests.Session()
        self.provider_session.headers.update({"User-Agent": USER_AGENT})
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        })
        self.log = logging.getLogger("tlp_proxy_worker")
        self.api_root = f"{GITHUB_API}/repos/{self.repository}"

    def _contents_url(self, path: str) -> str:
        safe_path = "/".join(quote(part, safe="") for part in path.strip("/").split("/"))
        return f"{self.api_root}/contents/{safe_path}"

    def list_json_files(self, directory: str) -> list[dict[str, Any]]:
        try:
            response = self.session.get(
                self._contents_url(directory),
                params={"ref": self.branch, "per_page": 100},
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            self.log.warning("GitHub list failed for %s (%s)", directory, type(exc).__name__)
            return []

        if response.status_code == 404:
            return []
        if response.status_code != 200:
            self.log.warning("GitHub list failed for %s: HTTP %s", directory, response.status_code)
            return []

        try:
            items = response.json()
        except ValueError:
            self.log.warning("GitHub returned invalid JSON while listing %s", directory)
            return []
        if not isinstance(items, list):
            return []
        return sorted(
            [item for item in items if isinstance(item, dict)
             and item.get("type") == "file"
             and str(item.get("name", "")).lower().endswith(".json")],
            key=lambda item: str(item.get("name", "")),
        )

    def get_text_file(self, path: str) -> tuple[Optional[str], Optional[str], int]:
        try:
            response = self.session.get(
                self._contents_url(path),
                params={"ref": self.branch},
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            self.log.warning("GitHub read failed for %s (%s)", path, type(exc).__name__)
            return None, None, 0

        if response.status_code != 200:
            if response.status_code in (401, 403):
                self.log.error("GitHub contents read rejected: HTTP %s path=%s; check Actions token Contents: write permission", response.status_code, path)
            return None, None, response.status_code
        try:
            data = response.json()
            if not isinstance(data, dict) or data.get("encoding") != "base64":
                return None, None, 502
            raw_content = str(data.get("content", "")).replace("\n", "").strip()
            content = base64.b64decode(raw_content, validate=True).decode("utf-8")
            return content, data.get("sha"), 200
        except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError):
            self.log.warning("Could not decode repository file %s", path)
            return None, None, 502

    def put_text_file(self, path: str, content: str) -> bool:
        """Create/update a GitHub Contents file; retry one SHA conflict."""
        url = self._contents_url(path)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        for attempt in range(2):
            _old_content, sha, status = self.get_text_file(path)
            if status not in (200, 404):
                self.log.warning("Cannot prepare write for %s: HTTP %s", path, status)
                return False

            payload: dict[str, Any] = {
                "message": f"Proxy response: {path.rsplit('/', 1)[-1]}",
                "content": encoded,
                "branch": self.branch,
            }
            if sha:
                payload["sha"] = sha
            try:
                response = self.session.put(url, json=payload, timeout=(5, 20))
            except requests.RequestException as exc:
                self.log.warning("GitHub write failed for %s (%s)", path, type(exc).__name__)
                return False

            if response.status_code in (200, 201):
                return True
            if response.status_code == 409 and attempt == 0:
                continue
            self.log.warning("GitHub write failed for %s: HTTP %s", path, response.status_code)
            return False
        return False

    def delete_file(self, path: str) -> bool:
        _content, sha, status = self.get_text_file(path)
        if status == 404:
            return True
        if status != 200 or not sha:
            return False
        payload = {
            "message": f"Remove processed queue item: {path.rsplit('/', 1)[-1]}",
            "sha": sha,
            "branch": self.branch,
        }
        try:
            response = self.session.delete(
                self._contents_url(path), json=payload, timeout=(5, 15)
            )
            return response.status_code in (200, 204, 404)
        except requests.RequestException as exc:
            self.log.warning("GitHub delete failed for %s (%s)", path, type(exc).__name__)
            return False

    @staticmethod
    def _provider_error(code: str) -> str:
        # The TLP bot recognizes this marker and displays its generic outage notice.
        # Never copy raw provider error bodies (which can contain sensitive details).
        return f"AI_BACKEND_UNAVAILABLE: {code}"

    def generate_reply(self, request_data: Any) -> str:
        if not isinstance(request_data, dict):
            return self._provider_error("INVALID_REQUEST")

        api_key = decode_api_key(request_data.get("api_key"))
        if not api_key:
            # Optional secure deployment path; current TLP versions still send api_key.
            api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
        if not api_key:
            return self._provider_error("MISSING_API_KEY")

        prompt = request_data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return self._provider_error("EMPTY_PROMPT")

        model = str(request_data.get("model") or "gemini-3.5-flash-lite").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,120}", model):
            return self._provider_error("INVALID_MODEL")

        payload = {"contents": [{"parts": [{"text": prompt[:180000]}]}]}
        url = f"{GEMINI_API}/{quote(model, safe='')}:generateContent"
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }

        try:
            response = self.provider_session.post(
                url,
                json=payload,
                headers=headers,
                timeout=(8, 55),
            )
        except requests.RequestException as exc:
            self.log.warning("Gemini request failed (%s)", type(exc).__name__)
            return self._provider_error("NETWORK")

        if response.status_code != 200:
            # Log the exact provider status and safe Google error fields so the
            # host can diagnose auth/quota/model failures without leaking the key.
            try:
                payload_error = response.json().get("error", {})
            except Exception:
                payload_error = {}
            if not isinstance(payload_error, dict):
                payload_error = {}
            details = payload_error.get("details") or []
            reason = ""
            for detail in details:
                if isinstance(detail, dict) and detail.get("reason"):
                    reason = str(detail.get("reason"))[:100]
                    break
            message = " ".join(str(payload_error.get("message") or "").split())[:220]
            self.log.error(
                "Gemini provider error: HTTP %s status=%s reason=%s message=%s",
                response.status_code,
                str(payload_error.get("status") or "-")[:80],
                reason or "-",
                message or "-",
            )
            return self._provider_error(f"HTTP_{response.status_code}")

        try:
            data = response.json()
        except ValueError:
            self.log.warning("Gemini returned malformed JSON")
            return self._provider_error("INVALID_PROVIDER_RESPONSE")

        reply = extract_gemini_text(data)
        if not reply:
            self.log.warning("Gemini response contained no text candidate")
            return self._provider_error("EMPTY_PROVIDER_RESPONSE")
        return reply[:30000]

    def process_request_file(self, request_path: str) -> bool:
        message_id = request_path.rsplit("/", 1)[-1].removesuffix(".json")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", message_id):
            self.log.warning("Skipping invalid queue filename")
            return False

        response_path = f"responses/{message_id}.json"

        # If a previous run saved the reply but failed to delete the request,
        # do not call Gemini twice.
        _existing_response, _sha, response_status = self.get_text_file(response_path)
        if response_status == 200:
            self.delete_file(request_path)
            return True
        if response_status not in (0, 404):
            self.log.warning("Could not check response slot for %s: HTTP %s", message_id, response_status)
            return False
        if response_status == 0:
            return False

        raw_request, _request_sha, status = self.get_text_file(request_path)
        if status != 200 or raw_request is None:
            self.log.warning("Could not read queue item %s: HTTP %s", message_id, status)
            return False

        request_data = None
        try:
            request_data = json.loads(raw_request)
        except (ValueError, TypeError):
            reply = self._provider_error("INVALID_QUEUE_JSON")
        else:
            reply = self.generate_reply(request_data)

        error_code = ""
        if isinstance(reply, str) and reply.startswith("AI_BACKEND_UNAVAILABLE:"):
            error_code = reply.split(":", 1)[-1].strip()[:100]
        response_body = json.dumps({
            "reply": reply,
            "error_code": error_code,
            "model": str((request_data.get("model") if isinstance(request_data, dict) else "") or "gemini-3.5-flash-lite"),
            "provider": "Gemini API",
        }, ensure_ascii=False)
        if not self.put_text_file(response_path, response_body):
            # Keep the request for a later pass if saving the response fails.
            return False

        self.delete_file(request_path)
        self.log.info("Processed queue item %s", message_id)
        return True

    def process_pending_requests(self, max_batch: int = 12) -> int:
        items = self.list_json_files("requests")
        processed = 0
        for item in items[:max_batch]:
            name = str(item.get("name", ""))
            if not name:
                continue
            if self.process_request_file(f"requests/{name}"):
                processed += 1
        return processed

    def run_forever(self) -> int:
        """Poll this repository until the Ranflare controller switches away.

        GitHub Actions is intentionally not allowed to restart itself. The bot
        on Ranflare chooses the active repository, starts the next worker, then
        cancels this run. This keeps exactly one configured repository active.
        """
        poll_seconds = _env_float("POLL_SECONDS", 2.0, 1.0, 15.0)
        max_runtime = _env_int("WORKER_MAX_RUNTIME_SECONDS", 21300, 600, 21300)
        started = time.monotonic()
        self.log.info("TLP GitHub proxy worker started for %s (branch %s)", self.repository, self.branch)
        self.log.info("Ranflare controls repository rotation; worker will not self-dispatch")
        self.log.info("Queue polling interval: %.1fs", poll_seconds)

        while True:
            elapsed = time.monotonic() - started
            if elapsed >= max_runtime:
                self.log.warning("Worker reached its safety runtime limit; waiting for Ranflare rotation")
                return 0
            try:
                self.process_pending_requests()
            except Exception as exc:  # Keep the worker alive after an isolated item failure.
                self.log.exception("Queue cycle failed (%s)", type(exc).__name__)
            remaining = max_runtime - (time.monotonic() - started)
            if remaining <= 0:
                return 0
            time.sleep(min(poll_seconds, remaining))


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = os.getenv("GH_TOKEN", "").strip()
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    branch = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"

    if not token or not repository:
        logging.error("Missing GH_TOKEN or GITHUB_REPOSITORY environment variable")
        return 2

    try:
        worker = ProxyWorker(token=token, repository=repository, branch=branch)
    except ValueError as exc:
        logging.error("Worker configuration error: %s", exc)
        return 2
    return worker.run_forever()


if __name__ == "__main__":
    sys.exit(main())
