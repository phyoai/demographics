import os
import sys
import unittest
from unittest.mock import AsyncMock, patch


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from BrightScraper.services import google_search_headless  # noqa: E402


class GoogleSearchHeadlessTests(unittest.TestCase):
    def test_generate_duckduckgo_urls_falls_back_without_openai_client(self):
        with patch.object(google_search_headless, "client", None):
            result = google_search_headless.generate_google_urls_from_prompt(
                "need 6 food influencers in gurugram, india around 100k followers",
                n=4,
            )

        self.assertEqual(result["limit"], 6)
        self.assertEqual(result["city"], "Gurugram")
        self.assertEqual(result["country"], "India")
        self.assertEqual(len(result["queries"]), 4)
        self.assertTrue(
            all(query.startswith("site:instagram.com") for query in result["queries"])
        )

    def test_extract_requested_profile_count_supports_number_words(self):
        result = google_search_headless._extract_requested_profile_count(
            "I want three food creators with 500k followers in delhi."
        )

        self.assertEqual(result, 3)


class RunUserSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_query_generation_returns_city_and_country_from_prompt(self):
        with patch.object(
            google_search_headless,
            "generate_google_urls_from_prompt",
            return_value={
                "limit": 4,
                "queries": ["site:instagram.com delhi beauty creators"],
            },
        ):
            result = await google_search_headless._generate_google_urls_from_prompt_async(
                "find 4 beauty creators in delhi, india",
                n=1,
            )

        self.assertEqual(result["city"], "Delhi")
        self.assertEqual(result["country"], "India")
        self.assertEqual(result["limit"], 4)
        self.assertEqual(result["queries"], ["site:instagram.com delhi beauty creators"])

    async def test_scrapes_two_queries_in_parallel_tabs(self):
        class FakeContext:
            async def close(self):
                return None

        class FakeBrowser:
            async def new_context(self, **kwargs):
                return FakeContext()

        active_workers = 0
        max_active_workers = 0

        async def fake_scrape(context, search_query):
            nonlocal active_workers, max_active_workers

            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
            await google_search_headless.asyncio.sleep(0.01)
            active_workers -= 1

            username = search_query.replace(" ", "_")
            return [
                {
                    "username": username,
                    "profile_url": f"https://www.instagram.com/{username}/",
                }
            ]

        generated_queries = ["query_one", "query_two", "query_three"]
        generate_mock = AsyncMock(
            return_value={
                "limit": 10,
                "queries": generated_queries,
            }
        )

        with (
            patch.object(
                google_search_headless,
                "SEARCH_PARALLEL_QUERY_TABS",
                2,
            ),
            patch.object(
                google_search_headless,
                "_generate_google_urls_from_prompt_async",
                generate_mock,
            ),
            patch.object(
                google_search_headless,
                "_ensure_browser",
                AsyncMock(return_value=FakeBrowser()),
            ),
            patch.object(
                google_search_headless,
                "_scrape_profiles_from_single_query",
                fake_scrape,
            ),
            patch.object(
                google_search_headless,
                "save_profiles_to_mongodb",
                return_value={},
            ),
        ):
            result = await google_search_headless.run_user_search(
                "need 6 food influencers"
            )

        self.assertEqual(len(result), 3)
        self.assertEqual(max_active_workers, 2)
        generate_mock.assert_awaited_once()
        self.assertGreaterEqual(generate_mock.await_args.kwargs["n"], 2)

    async def test_run_user_search_keeps_explicit_word_count_over_llm_limit(self):
        class FakeContext:
            async def close(self):
                return None

        class FakeBrowser:
            async def new_context(self, **kwargs):
                return FakeContext()

        async def fake_scrape(context, search_query):
            return [
                {
                    "username": f"user_{index}",
                    "profile_url": f"https://www.instagram.com/user_{index}/",
                    "followers_count": 800000,
                }
                for index in range(1, 6)
            ]

        with (
            patch.object(
                google_search_headless,
                "_generate_google_urls_from_prompt_async",
                AsyncMock(
                    return_value={
                        "limit": 10,
                        "queries": ["food creators delhi"],
                    }
                ),
            ),
            patch.object(
                google_search_headless,
                "_ensure_browser",
                AsyncMock(return_value=FakeBrowser()),
            ),
            patch.object(
                google_search_headless,
                "_scrape_profiles_from_single_query",
                fake_scrape,
            ),
            patch.object(
                google_search_headless,
                "save_profiles_to_mongodb",
                return_value={},
            ),
        ):
            result = await google_search_headless.run_user_search(
                "I want three food creators with 500k followers in delhi."
            )

        self.assertEqual(len(result), 3)


if __name__ == "__main__":
    unittest.main()
