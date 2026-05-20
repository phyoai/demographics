import json
import os
import sys
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from BrightScraper.instagram.location_predictor import InstagramLocationPredictor


class InstagramLocationPredictorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.predictor = InstagramLocationPredictor()

    def test_sample_payload_predicts_india_without_forcing_city(self):
        sample_path = os.path.join(REPO_ROOT, "data.json")
        with open(sample_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        result = self.predictor.infer(payload)

        self.assertEqual(result["country"], "India")
        self.assertGreaterEqual(result["country_confidence"], 0.72)
        self.assertIsNone(result["city"])
        self.assertEqual(result["prediction_mode"], "rule_based_profile_location")

    def test_explicit_city_signal_promotes_city_and_country(self):
        payload = {
            "result": {
                "profile": {
                    "username": "demo_creator",
                    "bio": "Comedian from Jaipur, India",
                    "full_name": "Demo Creator",
                    "external_links": [],
                    "highlight_titles": [],
                    "recent_posts": [],
                },
                "posts": [],
            }
        }

        result = self.predictor.infer(payload)

        self.assertEqual(result["city"], "Jaipur")
        self.assertEqual(result["country"], "India")
        self.assertGreaterEqual(result["city_confidence"], 0.72)


if __name__ == "__main__":
    unittest.main()
