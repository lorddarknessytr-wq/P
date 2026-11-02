import base64
import json
import unittest
from unittest.mock import patch
import os
from urllib.parse import unquote, urlparse

from worker import ProxyWorker, decode_api_key, extract_gemini_text


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeGitHubSession:
    def __init__(self):
        self.headers = {}
        self.files = {}
        self.counter = 0
        self.last_dispatch_headers = None

    @staticmethod
    def _path(url):
        marker = "/contents/"
        return unquote(url.split(marker, 1)[1]) if marker in url else ""

    def get(self, url, params=None, timeout=None):
        path = self._path(url)
        if path in ("requests", "responses"):
            rows = [
                {"name": name.rsplit("/", 1)[-1], "type": "file"}
                for name in self.files
                if name.startswith(path + "/") and name.endswith(".json")
            ]
            return FakeResponse(200, rows)
        if path not in self.files:
            return FakeResponse(404, {"message": "Not Found"})
        content, sha = self.files[path]
        return FakeResponse(200, {
            "encoding": "base64",
            "content": base64.b64encode(content.encode()).decode(),
            "sha": sha,
        })

    def put(self, url, json=None, timeout=None):
        path = self._path(url)
        self.counter += 1
        content = base64.b64decode(json["content"]).decode()
        self.files[path] = (content, f"sha-{self.counter}")
        return FakeResponse(201, {})

    def delete(self, url, json=None, timeout=None):
        path = self._path(url)
        self.files.pop(path, None)
        return FakeResponse(200, {})

    def post(self, url, json=None, timeout=None):
        self.last_dispatch_headers = dict(self.headers)
        return FakeResponse(204, {})


class FakeGeminiSession:
    def __init__(self, response=None):
        self.headers = {}
        self.response = response or FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": "سلام، پاسخ آزمایشی."}]}}]
        })
        self.call = None

    def post(self, url, json=None, headers=None, timeout=None):
        self.call = {"url": url, "json": json, "headers": headers or {}, "timeout": timeout}
        return self.response


class ProxyWorkerTests(unittest.TestCase):
    def test_decode_api_key(self):
        encoded = base64.b64encode(b"sample-key-123").decode()
        self.assertEqual(decode_api_key(encoded), "sample-key-123")
        self.assertIsNone(decode_api_key("not-base64!"))

    def test_extract_gemini_text_joins_parts(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "A"}, {"text": "B"}]}}]}
        self.assertEqual(extract_gemini_text(payload), "AB")
        self.assertEqual(extract_gemini_text({"error": "x"}), "")

    def test_gemini_request_uses_api_key_header_not_url_query(self):
        gh = FakeGitHubSession()
        provider = FakeGeminiSession()
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh, provider_session=provider)
        request = {
            "api_key": base64.b64encode(b"gemini-secret").decode(),
            "model": "gemini-test-model",
            "prompt": "hello",
        }
        self.assertEqual(worker.generate_reply(request), "سلام، پاسخ آزمایشی.")
        self.assertEqual(provider.call["headers"]["x-goog-api-key"], "gemini-secret")
        self.assertNotIn("key=", provider.call["url"])
        self.assertNotIn("Authorization", provider.call["headers"])
        self.assertIn("Authorization", gh.headers)
        self.assertNotIn("Authorization", provider.headers)

    def test_provider_failure_does_not_return_raw_provider_body(self):
        gh = FakeGitHubSession()
        provider = FakeGeminiSession(FakeResponse(401, {"error": {"message": "private detail"}}))
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh, provider_session=provider)
        request = {
            "api_key": base64.b64encode(b"bad-key").decode(),
            "model": "gemini-test-model",
            "prompt": "hello",
        }
        reply = worker.generate_reply(request)
        self.assertIn("AI_BACKEND_UNAVAILABLE", reply)
        self.assertIn("401", reply)
        self.assertNotIn("private detail", reply)

    def test_process_queue_writes_response_then_deletes_request(self):
        gh = FakeGitHubSession()
        provider = FakeGeminiSession()
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh, provider_session=provider)
        message_id = "12345678-abcd-1234-abcd-123456789abc"
        request = {
            "api_key": base64.b64encode(b"gemini-secret").decode(),
            "model": "gemini-test-model",
            "prompt": "hello",
        }
        gh.files[f"requests/{message_id}.json"] = (json.dumps(request), "request-sha")
        self.assertEqual(worker.process_pending_requests(), 1)
        self.assertNotIn(f"requests/{message_id}.json", gh.files)
        saved = json.loads(gh.files[f"responses/{message_id}.json"][0])
        self.assertEqual(saved["reply"], "سلام، پاسخ آزمایشی.")

    def test_existing_response_prevents_duplicate_gemini_call(self):
        gh = FakeGitHubSession()
        provider = FakeGeminiSession()
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh, provider_session=provider)
        message_id = "12345678-abcd-1234-abcd-123456789abc"
        gh.files[f"requests/{message_id}.json"] = ("{}", "request-sha")
        gh.files[f"responses/{message_id}.json"] = (json.dumps({"reply": "already done"}), "response-sha")
        self.assertTrue(worker.process_request_file(f"requests/{message_id}.json"))
        self.assertIsNone(provider.call)
        self.assertNotIn(f"requests/{message_id}.json", gh.files)

    def test_worker_exits_without_self_dispatch(self):
        gh = FakeGitHubSession()
        worker = ProxyWorker("gh-test-token", "owner/P", branch="main", session=gh)
        worker.process_pending_requests = lambda max_batch=12: 0
        self.assertFalse(hasattr(worker, "dispatch_replacement_worker"))
        with patch.dict(os.environ, {"WORKER_MAX_RUNTIME_SECONDS": "600", "POLL_SECONDS": "1"}), \
             patch("worker.time.monotonic", side_effect=[0.0, 0.0, 601.0]):
            self.assertEqual(worker.run_forever(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
