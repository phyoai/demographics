import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class DemographicsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api_module = None
        cls.import_error = None
        cls._injected_modules: list[str] = []
        os.environ["PHYO_SERVER_API_KEY"] = "test-api-key"
        os.environ["ANALYZE_DEFAULT_DEADLINE_SECONDS"] = "30"
        os.environ["LOGFIRE_DISABLED"] = "true"

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
                return {
                    "profile": {"username": profile_data.get("username", "")},
                    "analytics": {"ageRange": {"18-24": 60.0, "25-34": 40.0}},
                    "metrics": {"commentsAnalyzed": len(comments)},
                }

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
        self.api_module.app.state.analyze_jobs = {}
        self.api_module.app.state.analyze_tasks = {}

    def tearDown(self):
        if hasattr(self, "client_context"):
            self.client_context.__exit__(None, None, None)

    def test_sync_analyze_returns_cached_payload_without_recomputing(self):
        cached_payload = {
            "success": True,
            "status": "success",
            "error_code": None,
            "warnings": [],
            "timings": {"total_seconds": 0.5},
            "retry_summary": {},
            "data": {"profile": {"username": "demo"}},
            "saved_to_file": None,
        }

        with patch.object(
            self.api_module,
            "load_analyze_cache_from_db",
            return_value=(cached_payload, 12),
        ), patch.object(self.api_module, "execute_analysis_pipeline") as execute_mock:
            response = self.client.post(
                "/demographics/analyze",
                json={"username": "Demo"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["cache"]["hit"])
        self.assertEqual(payload["cache"]["source"], "db")
        execute_mock.assert_not_called()

    def test_sync_analyze_runs_pipeline_and_stores_cache(self):
        fresh_payload = {
            "success": True,
            "status": "success",
            "error_code": None,
            "warnings": [],
            "timings": {"total_seconds": 1.2},
            "retry_summary": {"total_http_attempts": 1},
            "data": {"profile": {"username": "demo"}},
            "saved_to_file": None,
        }

        with patch.object(
            self.api_module,
            "load_analyze_cache_from_db",
            return_value=(None, None),
        ), patch.object(
            self.api_module,
            "load_analyze_cache",
            return_value=(None, None),
        ), patch.object(
            self.api_module,
            "execute_analysis_pipeline",
            return_value=(fresh_payload, 200),
        ) as execute_mock, patch.object(
            self.api_module,
            "save_analyze_cache_to_db",
            return_value=True,
        ) as save_db_mock, patch.object(
            self.api_module,
            "save_analyze_cache",
            return_value="api_cache/analyze_demo.json",
        ) as save_file_mock:
            response = self.client.post(
                "/demographics/analyze",
                json={"username": "Demo", "fast_mode": True},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["cache"]["hit"])
        self.assertTrue(payload["cache"]["stored_in_db"])
        self.assertEqual(payload["cache"]["cache_file"], "api_cache/analyze_demo.json")
        execute_mock.assert_called_once_with("demo", 4, 30, True)
        save_db_mock.assert_called_once()
        save_file_mock.assert_called_once()

    def test_sync_analyze_uses_stored_data_without_cache_or_live_pipeline(self):
        stored_payload = {
            "success": True,
            "status": "success",
            "error_code": None,
            "warnings": [],
            "timings": {"total_seconds": 0.2},
            "retry_summary": {},
            "data": {"profile": {"username": "demo"}},
            "stored_data": {
                "db": "instagpy",
                "collection": "instagram_scrapes",
                "posts_loaded": 2,
                "posts_used_for_comments": 2,
                "comments_loaded": 10,
                "used_all_posts": True,
                "used_all_stored_comments": True,
            },
            "saved_to_file": None,
        }

        with patch.object(
            self.api_module,
            "execute_stored_analysis_pipeline",
            return_value=(stored_payload, 200),
        ) as execute_stored_mock, patch.object(
            self.api_module,
            "execute_analysis_pipeline",
        ) as execute_live_mock, patch.object(
            self.api_module,
            "load_analyze_cache_from_db",
        ) as load_db_cache_mock, patch.object(
            self.api_module,
            "load_analyze_cache",
        ) as load_file_cache_mock:
            response = self.client.post(
                "/demographics/analyze",
                json={"username": "Demo", "use_stored_data": True},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["cache"]["source"], "stored_data")
        self.assertTrue(payload["stored_data"]["used_all_posts"])
        self.assertTrue(payload["stored_data"]["used_all_stored_comments"])
        self.assertEqual(
            payload["stored_data"]["posts_used_for_comments"],
            payload["stored_data"]["posts_loaded"],
        )
        execute_stored_mock.assert_called_once_with("demo", 30, True)
        execute_live_mock.assert_not_called()
        load_db_cache_mock.assert_not_called()
        load_file_cache_mock.assert_not_called()

    def test_sync_stored_analyze_passes_fast_mode_false_to_pipeline(self):
        stored_payload = {
            "success": True,
            "status": "success",
            "error_code": None,
            "warnings": [],
            "timings": {"total_seconds": 0.2},
            "retry_summary": {},
            "data": {"profile": {"username": "demo"}},
            "stored_data": {
                "posts_loaded": 2,
                "posts_used_for_comments": 2,
                "comments_loaded": 10,
            },
            "saved_to_file": None,
        }

        with patch.object(
            self.api_module,
            "execute_stored_analysis_pipeline",
            return_value=(stored_payload, 200),
        ) as execute_stored_mock:
            response = self.client.post(
                "/demographics/analyze",
                json={"username": "Demo", "use_stored_data": True, "fast_mode": False},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        execute_stored_mock.assert_called_once_with("demo", 30, False)

    def test_profiles_scrape_uses_stored_mongo_source_without_api_key(self):
        stored_payload = {
            "success": True,
            "status": "success",
            "error_code": None,
            "warnings": [],
            "timings": {"total_seconds": 0.2},
            "retry_summary": {},
            "data": {"profile": {"username": "therajivmakhni"}},
            "stored_data": {
                "db": "instagpy",
                "collection": "instagram_scrapes",
                "posts_loaded": 30,
                "posts_used_for_comments": 30,
                "comments_loaded": 925,
                "used_all_posts": True,
                "used_all_stored_comments": True,
            },
            "saved_to_file": None,
        }

        with patch.object(
            self.api_module,
            "execute_stored_analysis_pipeline",
            return_value=(stored_payload, 200),
        ) as execute_stored_mock:
            response = self.client.post(
                "/profiles/scrape",
                json={
                    "username": "therajivmakhni",
                    "max_posts": 30,
                    "max_comments": 50,
                    "post_workers": 2,
                    "force_refresh": True,
                    "mongodb_database": "instagpy",
                    "mongodb_collection": "instagram_scrapes",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["cache"]["source"], "stored_data")
        self.assertEqual(payload["profile_scrape_request"]["mongodb_database"], "instagpy")
        self.assertEqual(payload["profile_scrape_request"]["mongodb_collection"], "instagram_scrapes")
        self.assertFalse(payload["profile_scrape_request"]["fast_mode"])
        self.assertTrue(payload["profile_scrape_request"]["used_all_stored_posts_and_comments"])
        self.assertIn("Stored-data analysis used all posts", " ".join(payload["warnings"]))
        execute_stored_mock.assert_called_once_with(
            "therajivmakhni",
            30,
            False,
            database_name="instagpy",
            collection_name="instagram_scrapes",
        )

    def test_profiles_scrape_rejects_blank_username(self):
        response = self.client.post("/profiles/scrape", json={"username": "   "})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "Provide at least one username via 'username' or 'usernames'.",
        )

    def test_stored_scrape_normalization_uses_all_posts_and_comments(self):
        service_module = sys.modules["BrightScraper.services.demographics_analysis_service"]
        scrape_doc = {
            "_id": "stored-id",
            "requested_username": "demo",
            "result": {
                "profile": {
                    "username": "demo",
                    "full_name": "Demo User",
                    "bio": "Tech creator",
                    "followers_count": "1,200",
                    "following_count": 50,
                    "posts_count": 2,
                    "profile_pic_url": "https://example.com/profile.jpg",
                },
                "posts": [
                    {
                        "post_url": "https://instagram.com/p/one",
                        "shortcode": "one",
                        "post_type": "reel",
                        "caption": "Hello #tech",
                        "likes_count": "100",
                        "comments_count_total": 2,
                        "posted_at": "2026-05-24T05:06:06.000Z",
                        "comments": [
                            {
                                "username": "alice",
                                "text": "Great from Delhi",
                                "posted_at": "2026-05-24T06:00:00.000Z",
                                "likes_count": 3,
                                "profile_pic_url": "https://example.com/alice.jpg",
                            },
                            {
                                "user": {
                                    "username": "bob",
                                    "full_name": "Bob Smith",
                                    "profile_pic_url": "https://example.com/bob.jpg",
                                },
                                "comment_text": "Nice",
                                "created_at": "2026-05-24T06:10:00.000Z",
                                "likes": "4",
                            },
                        ],
                    },
                    {
                        "post_url": "https://instagram.com/p/two",
                        "shortcode": "two",
                        "post_type": "post",
                        "likes_count": 25,
                        "comments_count_total": 1,
                        "comments": [
                            {
                                "commenter_username": "carol",
                                "body": "Mumbai vibes",
                                "likes_count": 1,
                            }
                        ],
                    },
                ],
            },
        }

        profile, posts, comments, meta = service_module.build_stored_analysis_inputs(
            scrape_doc,
            "demo",
            database_name="instagpy",
            collection_name="instagram_scrapes",
        )

        self.assertEqual(profile["followers"], 1200)
        self.assertEqual(profile["biography"], "Tech creator")
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(comments), 3)
        self.assertEqual(comments[0]["post_url"], "https://instagram.com/p/one")
        self.assertEqual(comments[0]["profile_pic_url"], "https://example.com/alice.jpg")
        self.assertEqual(comments[1]["username"], "bob")
        self.assertEqual(comments[1]["full_name"], "Bob Smith")
        self.assertEqual(comments[2]["likes"], 1)
        self.assertEqual(meta["posts_loaded"], 2)
        self.assertEqual(meta["posts_used_for_comments"], 2)
        self.assertEqual(meta["comments_loaded"], 3)
        self.assertEqual(meta["db"], "instagpy")
        self.assertEqual(meta["collection"], "instagram_scrapes")

    def test_async_analyze_queues_job(self):
        def fake_create_task(coro):
            coro.close()
            return object()

        with patch.object(self.api_module.asyncio, "create_task", side_effect=fake_create_task) as create_task_mock:
            response = self.client.post(
                "/demographics/analyze",
                json={"username": "demo", "mode": "async", "deadline_seconds": 45},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "queued")
        self.assertIn("job_id", payload)
        self.assertIn("/analyze/jobs/", payload["status_url"])
        self.assertTrue(create_task_mock.called)

    def test_analyze_job_status_not_found(self):
        response = self.client.get("/analyze/jobs/does-not-exist", headers=self.headers)

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["error_code"], "NO_DATA")


if __name__ == "__main__":
    unittest.main()
