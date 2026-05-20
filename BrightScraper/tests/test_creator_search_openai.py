import os
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


from BrightScraper.instagram import creator_search_openai


class _FailingResponses:
    def create(self, **kwargs):
        raise RuntimeError("401 invalid_api_key")


class _FailingClient:
    def __init__(self):
        self.responses = _FailingResponses()


class CreatorSearchOpenAITests(unittest.TestCase):
    def test_build_fallback_search_arguments_extracts_limit_followers_and_city(self):
        arguments = creator_search_openai._build_fallback_search_arguments(
            "Find 10 food influencers in Delhi with at least 100k followers"
        )

        self.assertEqual(arguments["limit"], 10)
        self.assertEqual(arguments["min_followers"], 100000)
        self.assertIsNone(arguments["max_followers"])
        self.assertEqual(arguments["city"], "Delhi")
        self.assertIsNone(arguments["country"])
        self.assertEqual(arguments["query"], "food influencers in Delhi with at least 100k followers")

    def test_run_agent_falls_back_when_openai_client_is_unavailable(self):
        expected_result = [{"username": "foodie.delhi"}]

        with patch.object(
            creator_search_openai,
            "_build_openai_client",
            return_value=None,
        ), patch.object(
            creator_search_openai,
            "search_creator_profiles",
            return_value=expected_result,
        ) as search_mock:
            result = creator_search_openai.run_agent("10 food influencers")

        self.assertEqual(result, expected_result)
        self.assertEqual(search_mock.call_count, 1)
        self.assertEqual(search_mock.call_args.kwargs["limit"], 10)
        self.assertEqual(search_mock.call_args.kwargs["query"], "food influencers")

    def test_run_agent_falls_back_when_openai_request_fails(self):
        expected_result = [{"username": "foodie.india"}]

        with patch.object(
            creator_search_openai,
            "_build_openai_client",
            return_value=_FailingClient(),
        ), patch.object(
            creator_search_openai,
            "search_creator_profiles",
            return_value=expected_result,
        ) as search_mock:
            result = creator_search_openai.run_agent("10 food influencers in India")

        self.assertEqual(result, expected_result)
        self.assertEqual(search_mock.call_count, 1)
        self.assertEqual(search_mock.call_args.kwargs["limit"], 10)
        self.assertEqual(search_mock.call_args.kwargs["country"], "India")


if __name__ == "__main__":
    unittest.main()
