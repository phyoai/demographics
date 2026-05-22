import os
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


from BrightScraper.instagram import creator_search_openai
from BrightScraper.api_models import CreatorSearchRequest


class CreatorSearchTests(unittest.TestCase):
    def test_parse_creator_query_extracts_filters_and_semantic_query(self):
        parsed = creator_search_openai.parse_creator_query(
            "Find 10 food influencers in Delhi with at least 100k followers"
        )

        self.assertEqual(parsed.semantic_query, "food creator")
        self.assertEqual(parsed.country, "india")
        self.assertEqual(parsed.city, "delhi")
        self.assertEqual(parsed.niches, ["food"])
        self.assertEqual(parsed.min_followers, 100000)
        self.assertIsNone(parsed.max_followers)
        self.assertEqual(parsed.limit, 10)

    def test_parse_creator_query_extracts_max_followers_and_aliases(self):
        parsed = creator_search_openai.parse_creator_query(
            "fashion creators from Bombay under 50k followers"
        )

        self.assertEqual(parsed.city, "mumbai")
        self.assertEqual(parsed.country, "india")
        self.assertEqual(parsed.niches, ["fashion"])
        self.assertIsNone(parsed.min_followers)
        self.assertEqual(parsed.max_followers, 50000)

    def test_build_filter_targets_normalized_payload_fields(self):
        query_filter = creator_search_openai.build_filter(
            country="India",
            city="New Delhi",
            niches=["food blogger", "technology"],
            min_followers=10000,
            max_followers=50000,
        )

        self.assertIsNotNone(query_filter)
        conditions = query_filter.must
        self.assertEqual([condition.key for condition in conditions], ["country_norm", "city_norm", "niche_norm", "followers"])
        self.assertEqual(conditions[0].match.value, "india")
        self.assertEqual(conditions[1].match.value, "delhi")
        self.assertEqual(conditions[2].match.any, ["food", "tech"])
        self.assertEqual(conditions[3].range.gte, 10000)
        self.assertEqual(conditions[3].range.lte, 50000)

    def test_run_agent_forwards_explicit_overrides_to_search(self):
        expected_result = [{"username": "foodie.delhi"}]

        with patch.object(
            creator_search_openai,
            "search_creator_profiles",
            return_value=expected_result,
        ) as search_mock:
            result = creator_search_openai.run_agent(
                "food influencers",
                country="India",
                city="Delhi",
                niches=["food"],
                min_followers=100000,
                limit=5,
            )

        self.assertEqual(result, expected_result)
        search_mock.assert_called_once_with(
            query="food influencers",
            collection_name=None,
            country="India",
            city="Delhi",
            niches=["food"],
            min_followers=100000,
            max_followers=None,
            limit=5,
        )

    def test_creator_search_request_accepts_script_style_optional_fields(self):
        payload = CreatorSearchRequest(
            user_query="fashion creators in Delhi",
            collection="instagram_creator_profiles",
            niche="fashion",
            limit=5,
        )

        self.assertEqual(payload.collection, "instagram_creator_profiles")
        self.assertEqual(payload.niche, "fashion")
        self.assertEqual(payload.limit, 5)


if __name__ == "__main__":
    unittest.main()
