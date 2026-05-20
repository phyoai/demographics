import importlib
import os
import sys
import time
import types
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class InstagramProfileDataApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_module = None
        cls.import_error = None
        cls._injected_modules: list[str] = []
        os.environ["PHYO_SERVER_API_KEY"] = "test-api-key"

        def inject_module(name: str, module):
            if name not in sys.modules:
                sys.modules[name] = module
                cls._injected_modules.append(name)

        audience_module = types.ModuleType("BrightScraper.audience_analytics")

        class _AnalysisDeadlineExceeded(Exception):
            pass

        class _AudienceAnalytics:
            pass

        audience_module.AnalysisDeadlineExceeded = _AnalysisDeadlineExceeded
        audience_module.AudienceAnalytics = _AudienceAnalytics
        inject_module("BrightScraper.audience_analytics", audience_module)

        brightdata_module = types.ModuleType("BrightScraper.brightdata_client")

        class _BrightDataTimeoutError(Exception):
            pass

        class _BrightDataRateLimitError(Exception):
            pass

        class _BrightDataBadResponseError(Exception):
            pass

        brightdata_module.BrightDataTimeoutError = _BrightDataTimeoutError
        brightdata_module.BrightDataRateLimitError = _BrightDataRateLimitError
        brightdata_module.BrightDataBadResponseError = _BrightDataBadResponseError
        inject_module("BrightScraper.brightdata_client", brightdata_module)

        formatter_module = types.ModuleType("BrightScraper.enhanced_response_formatter")

        class _EnhancedResponseFormatter:
            def format_enhanced_response(self, profile_data, demographics, comments):
                return {"profile": {}, "analytics": {}, "metrics": {}}

        formatter_module.EnhancedResponseFormatter = _EnhancedResponseFormatter
        inject_module("BrightScraper.enhanced_response_formatter", formatter_module)

        apify_post_details_module = types.ModuleType("BrightScraper.instagram.apify_post_details")
        apify_post_details_module.INSTAGRAM_PROFILES_DATA_COLLECTION = "instagram_profiles_data"
        apify_post_details_module.fetch_and_store_profile_data_blocking = lambda usernames: []
        apify_post_details_module.get_instagram_profiles_data_collection = lambda: None
        apify_post_details_module.load_instagram_profile_data_from_db = lambda username: None
        apify_post_details_module.normalize_username = lambda value: str(value).strip().lstrip("@").strip("/").lower() or None
        inject_module("BrightScraper.instagram.apify_post_details", apify_post_details_module)

        age_fixer_module = types.ModuleType("BrightScraper.utils.age_gropu_fixer")
        age_fixer_module.redistribute_to_zero_groups = lambda payload: payload
        inject_module("BrightScraper.utils.age_gropu_fixer", age_fixer_module)

        db_conn_module = types.ModuleType("BrightScraper.utils.build_db_conn")
        db_conn_module.build_db_conn = lambda: None
        inject_module("BrightScraper.utils.build_db_conn", db_conn_module)

        google_search_module = types.ModuleType("BrightScraper.services.google_search_headless")
        google_search_module.run_user_search = lambda query: []
        inject_module("BrightScraper.services.google_search_headless", google_search_module)

        try:
            from BrightScraper import api as api_module  # noqa: F401

            cls.api_module = importlib.reload(api_module)
        except Exception as exc:  # pragma: no cover
            cls.import_error = exc

    @classmethod
    def tearDownClass(cls):
        for module_name in reversed(getattr(cls, "_injected_modules", [])):
            sys.modules.pop(module_name, None)

    def setUp(self):
        if self.import_error is not None:  # pragma: no cover
            self.skipTest(f"api module unavailable in this runtime: {self.import_error}")
        self.client_context = TestClient(self.api_module.app)
        self.client = self.client_context.__enter__()
        self.headers = {"api-key": "test-api-key"}

    def tearDown(self):
        if hasattr(self, "client_context"):
            self.client_context.__exit__(None, None, None)

    def test_profile_data_bulk_only_fetches_missing_usernames(self):
        documents = {
            "cached_user": {
                "username": "cached_user",
                "fullName": "Cached User",
                "updated_at_epoch": int(time.time()),
            },
        }

        def fake_load(username):
            return documents.get(username)

        def fake_fetch(usernames):
            documents["fresh_user"] = {"username": "fresh_user", "fullName": "Fresh User"}
            return [documents["fresh_user"]]

        with patch.object(self.api_module, "get_instagram_profiles_data_collection", return_value=object()), patch.object(
            self.api_module,
            "load_instagram_profile_data_from_db",
            side_effect=fake_load,
        ), patch.object(
            self.api_module,
            "fetch_and_store_profile_data_blocking",
            side_effect=fake_fetch,
        ) as fetch_mock:
            response = self.client.post(
                "/instagram/profile-data",
                json={"usernames": ["cached_user", "Fresh_User", "cached_user"]},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["requested_usernames"], ["cached_user", "fresh_user"])
        self.assertEqual(payload["db_usernames"], ["cached_user"])
        self.assertEqual(payload["fetched_usernames"], ["fresh_user"])
        self.assertEqual(payload["not_found_usernames"], [])
        self.assertEqual(payload["fetched_count"], 1)
        self.assertEqual(payload["profiles"]["cached_user"]["source"], "db")
        self.assertEqual(payload["profiles"]["fresh_user"]["source"], "apify")
        fetch_mock.assert_called_once_with(["fresh_user"])

    def test_profile_data_bulk_refreshes_stale_usernames_older_than_a_week(self):
        stale_epoch = int(time.time()) - (8 * 24 * 60 * 60)
        documents = {
            "stale_user": {
                "username": "stale_user",
                "fullName": "Stale User",
                "updated_at_epoch": stale_epoch,
            },
            "fresh_user": {
                "username": "fresh_user",
                "fullName": "Fresh User",
                "updated_at_epoch": int(time.time()),
            },
        }

        def fake_load(username):
            return documents.get(username)

        def fake_fetch(usernames):
            documents["stale_user"] = {
                "username": "stale_user",
                "fullName": "Regenerated User",
                "updated_at_epoch": int(time.time()),
            }
            return [documents["stale_user"]]

        with patch.object(self.api_module, "get_instagram_profiles_data_collection", return_value=object()), patch.object(
            self.api_module,
            "load_instagram_profile_data_from_db",
            side_effect=fake_load,
        ), patch.object(
            self.api_module,
            "fetch_and_store_profile_data_blocking",
            side_effect=fake_fetch,
        ) as fetch_mock:
            response = self.client.post(
                "/instagram/profile-data",
                json={"usernames": ["stale_user", "fresh_user"]},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["db_usernames"], ["fresh_user"])
        self.assertEqual(payload["stale_usernames"], ["stale_user"])
        self.assertEqual(payload["fetched_usernames"], ["stale_user"])
        self.assertEqual(payload["refreshed_usernames"], ["stale_user"])
        self.assertEqual(payload["profiles"]["stale_user"]["source"], "apify")
        self.assertEqual(payload["profiles"]["stale_user"]["data"]["fullName"], "Regenerated User")
        fetch_mock.assert_called_once_with(["stale_user"])

    def test_analytics_uses_instagram_profile_data_documents(self):
        resolved_payload = {
            "db_usernames": ["cached_user"],
            "fetched_usernames": ["fresh_user"],
            "profiles": {
                "cached_user": {
                    "source": "db",
                    "data": {
                        "username": "cached_user",
                        "fullName": "Cached User",
                        "followersCount": 1000,
                        "followsCount": 25,
                        "postsCount": 250,
                        "verified": True,
                        "latestPosts": [
                            {
                                "shortCode": "POST1",
                                "url": "https://www.instagram.com/p/POST1/",
                                "type": "Image",
                                "timestamp": "2026-04-10T10:00:00.000Z",
                                "likesCount": 100,
                                "commentsCount": 10,
                            },
                            {
                                "shortCode": "POST2",
                                "url": "https://www.instagram.com/p/POST2/",
                                "type": "Video",
                                "timestamp": "2026-04-11T10:00:00.000Z",
                                "likesCount": 200,
                                "commentsCount": 20,
                                "videoViewCount": 1000,
                            },
                        ],
                    },
                },
                "fresh_user": {
                    "source": "apify",
                    "data": {
                        "username": "fresh_user",
                        "fullName": "Fresh User",
                        "followersCount": 500,
                        "followsCount": 10,
                        "postsCount": 50,
                        "verified": False,
                        "latestPosts": [
                            {
                                "shortCode": "POST3",
                                "url": "https://www.instagram.com/p/POST3/",
                                "type": "Image",
                                "timestamp": "2026-04-12T10:00:00.000Z",
                                "likesCount": 50,
                                "commentsCount": 5,
                            }
                        ],
                    },
                },
            },
        }

        with patch.object(
            self.api_module,
            "resolve_instagram_profile_data_usernames",
            return_value=resolved_payload,
        ) as resolve_mock:
            response = self.client.post(
                "/analytics",
                json={"usernames": ["cached_user", "fresh_user", "missing_user"]},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["found_usernames"], ["cached_user", "fresh_user"])
        self.assertEqual(payload["not_found_usernames"], ["missing_user"])
        self.assertEqual(payload["db_usernames"], ["cached_user"])
        self.assertEqual(payload["fetched_usernames"], ["fresh_user"])
        self.assertEqual(payload["analytics"]["cached_user"]["profile"]["followers"], 1000)
        self.assertEqual(payload["analytics"]["cached_user"]["overall"]["posts"], 2)
        self.assertEqual(payload["analytics"]["cached_user"]["overall"]["totals"]["likes"], 300)
        self.assertEqual(payload["analytics"]["cached_user"]["overall"]["totals"]["comments"], 30)
        self.assertEqual(payload["analytics"]["cached_user"]["overall"]["totals"]["views"], 1000)
        self.assertEqual(payload["analytics"]["cached_user"]["overall"]["totals"]["engagement"], 1330)
        self.assertEqual(
            payload["analytics"]["cached_user"]["overall"]["rates"]["engagement_rate_percent"],
            133.0,
        )
        self.assertEqual(
            payload["analytics"]["cached_user"]["overall"]["rates"]["avg_engagement_rate_percent"],
            66.5,
        )
        self.assertEqual(
            payload["analytics"]["cached_user"]["latest_top_post_by_engagement"]["post"]["shortcode"],
            "POST2",
        )
        self.assertEqual(
            payload["analytics"]["cached_user"]["latest_top_post_by_engagement"]["post"]["engagement_rate_percent"],
            122.0,
        )
        resolve_mock.assert_called_once_with(["cached_user", "fresh_user", "missing_user"])

    def test_search_users_rejects_blank_prompt(self):
        response = self.client.post(
            "/instagram/search-users",
            json={"prompt": "   "},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "prompt is required")

    def test_search_users_returns_serializable_http_error_when_service_fails(self):
        with patch.object(
            self.api_module,
            "run_user_search",
            side_effect=RuntimeError("query generation failed"),
        ):
            response = self.client.post(
                "/instagram/search-users",
                json={"prompt": "need 6 food influencers in gurugram"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["detail"],
            "Instagram user search failed: query generation failed",
        )

    def test_search_users_schedules_profile_scrape_trigger_from_result_usernames(self):
        search_results = [
            {"username": "just.jully", "profile_url": "https://www.instagram.com/just.jully/"},
            {"username": "@foodie.delhi", "profile_url": "https://www.instagram.com/foodie.delhi/"},
            {"username": "JUST.JULLY", "profile_url": "https://www.instagram.com/just.jully/"},
        ]

        with (
            patch.object(
                self.api_module,
                "run_user_search",
                return_value=search_results,
            ),
            patch.object(
                self.api_module,
                "_schedule_profile_scrape_trigger",
            ) as schedule_mock,
        ):
            response = self.client.post(
                "/instagram/search-users",
                json={"prompt": "I want three food creators with 500k followers in delhi."},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), search_results)
        schedule_mock.assert_called_once_with(["just.jully", "foodie.delhi"])

    def test_locations_resolve_handles_batch_payload(self):
        expected_payload = {
            "requested_count": 2,
            "resolved_count": 2,
            "failed_count": 0,
            "parallelism": 2,
            "limit": 1,
            "timings": {"total_seconds": 0.021},
            "results": [
                {
                    "index": 0,
                    "success": True,
                    "query": "Delhi, Delhi, India",
                    "resolved_query": "Delhi, Delhi, India",
                    "result_count": 1,
                    "results": [{"lat": "28.6", "lon": "77.2"}],
                    "error": None,
                    "elapsed_seconds": 0.01,
                },
                {
                    "index": 1,
                    "success": True,
                    "query": "Gurugram, Haryana, India",
                    "resolved_query": "Gurugram, Haryana, India",
                    "result_count": 1,
                    "results": [{"lat": "28.46", "lon": "77.03"}],
                    "error": None,
                    "elapsed_seconds": 0.011,
                },
            ],
        }

        with patch.object(
            self.api_module,
            "resolve_location_requests_service",
            return_value=expected_payload,
        ) as resolve_mock:
            response = self.client.post(
                "/locations/resolve",
                json={
                    "search_queries": [
                        "  Delhi, Delhi, India ",
                        "Gurugram, Haryana, India",
                    ],
                    "parallelism": 2,
                    "limit": 1,
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected_payload)
        resolve_mock.assert_called_once_with(
            queries=[
                "Delhi, Delhi, India",
                "Gurugram, Haryana, India",
            ],
            parallelism=2,
            limit=1,
        )

    def test_locations_resolve_rejects_empty_payload(self):
        response = self.client.post(
            "/locations/resolve",
            json={},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "Provide at least one search query via 'search_query' or 'search_queries'.",
        )

    def test_locations_resolve_rejects_city_state_country_payload(self):
        response = self.client.post(
            "/locations/resolve",
            json={
                "locations": [
                    {"city": "Delhi", "state": "Delhi", "country": "India"},
                ]
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)

    def test_locations_reverse_handles_batch_payload(self):
        expected_payload = {
            "requested_count": 2,
            "resolved_count": 2,
            "failed_count": 0,
            "parallelism": 2,
            "zoom": 15,
            "timings": {"total_seconds": 0.032},
            "results": [
                {
                    "index": 0,
                    "success": True,
                    "query": {"lat": 28.56348467449849, "lon": 77.152755, "zoom": 15},
                    "resolved_query": "28.56348467449849,77.152755",
                    "result_count": 1,
                    "results": [{"display_name": "Delhi, India"}],
                    "error": None,
                    "elapsed_seconds": 0.015,
                },
                {
                    "index": 1,
                    "success": True,
                    "query": {"lat": 28.4595, "lon": 77.0266, "zoom": 15},
                    "resolved_query": "28.4595,77.0266",
                    "result_count": 1,
                    "results": [{"display_name": "Gurugram, India"}],
                    "error": None,
                    "elapsed_seconds": 0.017,
                },
            ],
        }

        with patch.object(
            self.api_module,
            "resolve_reverse_location_requests_service",
            return_value=expected_payload,
        ) as reverse_mock:
            response = self.client.post(
                "/locations/reverse",
                json={
                    "points": [
                        {"lat": 28.56348467449849, "lon": 77.152755},
                        {"lat": 28.4595, "lon": 77.0266},
                    ],
                    "parallelism": 2,
                    "zoom": 15,
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected_payload)
        reverse_mock.assert_called_once_with(
            points=[
                {"lat": 28.56348467449849, "lon": 77.152755},
                {"lat": 28.4595, "lon": 77.0266},
            ],
            parallelism=2,
            zoom=15,
        )

    def test_locations_reverse_rejects_empty_payload(self):
        response = self.client.post(
            "/locations/reverse",
            json={},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "Provide at least one point via 'point' or 'points'.",
        )


if __name__ == "__main__":
    unittest.main()
