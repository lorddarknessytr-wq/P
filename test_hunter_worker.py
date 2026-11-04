import base64
import json
import unittest
from unittest.mock import patch

from worker import ProxyWorker, decode_api_key
from test_worker import FakeGitHubSession, FakeGeminiSession, FakeResponse


class HeaderResponse(FakeResponse):
    def __init__(self, status_code=200, payload=None, text=None):
        super().__init__(status_code, payload, text)
        self.headers = {}


class HunterWorkerTests(unittest.TestCase):
    def test_decision_queue_calls_jev_and_writes_separate_response(self):
        gh = FakeGitHubSession()
        provider = FakeGeminiSession(HeaderResponse(200, {
            "model": "jev-style-2b-decision-v4",
            "answers": {"target_match": {"type": "noul", "noul": 0.9}},
        }))
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh, provider_session=provider)
        request_id = "hunter-jev-0001"
        request = {"kind": "jev_decision",
                   "api_key": base64.b64encode(b"jev-private-key").decode("ascii"),
                   "payload": {"state": "listing", "questions": {"target_match": {"type": "noul", "instructions": "match?"}}}}
        gh.files[f"decision_requests/{request_id}.json"] = (json.dumps(request), "request-sha")
        self.assertTrue(worker.process_hunter_request_file(f"decision_requests/{request_id}.json", "decision_responses"))
        self.assertNotIn(f"decision_requests/{request_id}.json", gh.files)
        saved = json.loads(gh.files[f"decision_responses/{request_id}.json"][0])
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["provider"], "Jev-Style API")
        self.assertEqual(provider.call["url"], "https://api.jevstyle.com/v1/systemone")
        self.assertEqual(provider.call["headers"]["Authorization"], "Bearer jev-private-key")
        self.assertNotIn("jev-private-key", gh.files[f"decision_responses/{request_id}.json"][0])

    def test_market_queue_calls_gemini_and_keeps_tlp_queue_separate(self):
        gh = FakeGitHubSession()
        provider = FakeGeminiSession(HeaderResponse(200, {
            "candidates": [{"content": {"parts": [{"text": '{"eligible_ids":["a1"]}'}]}}]
        }))
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh, provider_session=provider)
        request_id = "hunter-market-0001"
        request = {"kind": "gemini_market",
                   "api_key": base64.b64encode(b"gemini-private-key").decode("ascii"),
                   "model": "gemini-3.8-flash", "prompt": "classify sample listings"}
        gh.files[f"market_requests/{request_id}.json"] = (json.dumps(request), "request-sha")
        self.assertTrue(worker.process_hunter_request_file(f"market_requests/{request_id}.json", "market_responses"))
        saved = json.loads(gh.files[f"market_responses/{request_id}.json"][0])
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["model"], "gemini-3.8-flash")
        self.assertIn("eligible_ids", saved["text"])
        self.assertEqual(provider.call["headers"]["x-goog-api-key"], "gemini-private-key")

    def test_hunter_directory_polling_is_throttled_without_changing_legacy_polling(self):
        gh = FakeGitHubSession()
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh)
        listed = []
        worker.list_json_files = lambda directory: (listed.append(directory) or [])
        for _ in range(4):
            worker.process_pending_requests()
        self.assertEqual(listed.count("requests"), 4)
        self.assertEqual(listed.count("decision_requests"), 2)
        self.assertEqual(listed.count("market_requests"), 2)

    def test_legacy_tlp_queue_path_is_unchanged(self):
        gh = FakeGitHubSession()
        worker = ProxyWorker("gh-test-token", "owner/P", session=gh)
        with patch.object(worker, "list_json_files", side_effect=lambda d: []):
            self.assertEqual(worker.process_pending_requests(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
