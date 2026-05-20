import os
import sys
import unittest
from unittest.mock import Mock


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from BrightScraper.brightdata_client import BrightDataClient, new_retry_summary  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class BrightDataClientTests(unittest.TestCase):
    def test_request_json_retries_retryable_status_then_succeeds(self):
        client = BrightDataClient(api_key="test-token", base_url="https://example.com", retry_profile="minimal")
        summary = new_retry_summary("minimal")

        responses = [
            FakeResponse(500, {"error": "server error"}),
            FakeResponse(200, {"ok": True}),
        ]
        client.session.request = Mock(side_effect=responses)

        payload = client._request_json(
            "GET",
            "https://example.com/ping",
            operation="unit_request",
            summary=summary,
            max_attempts=3,
            timeout=(1, 1),
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(summary["total_http_attempts"], 2)
        self.assertEqual(summary["total_http_retries"], 1)
        self.assertEqual(summary["operations"]["unit_request"]["attempts"], 2)

    def test_fetch_snapshot_data_polls_processing_then_ready(self):
        client = BrightDataClient(api_key="test-token", base_url="https://example.com", retry_profile="minimal")
        summary = new_retry_summary("minimal")

        calls = iter(
            [
                {"status": "running"},
                {"status": "processing"},
                {"status": "ready", "data": [{"id": 1}]},
            ]
        )

        def fake_request_json(*args, **kwargs):
            return next(calls)

        client._request_json = fake_request_json  # type: ignore[assignment]

        data = client.fetch_snapshot_data(
            "snapshot_1",
            summary=summary,
            poll_retries=5,
            poll_delay=0.01,
            operation="unit_poll",
        )

        self.assertEqual(data, [{"id": 1}])
        self.assertEqual(summary["operations"]["unit_poll"]["poll_iterations"], 3)


if __name__ == "__main__":
    unittest.main()
