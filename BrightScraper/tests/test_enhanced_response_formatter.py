import os
import sys
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from BrightScraper.enhanced_response_formatter import EnhancedResponseFormatter


class EnhancedResponseFormatterTests(unittest.TestCase):
    def test_visible_gender_is_normalized_to_binary_total(self):
        formatter = EnhancedResponseFormatter()

        result = formatter.format_enhanced_response(
            profile_data={"username": "demo", "followers": 1000, "posts": []},
            demographics={
                "gender_distribution": {
                    "male": 36.6,
                    "female": 32.3,
                    "unknown": 31.1,
                },
                "age_distribution": {},
                "country_distribution": {},
                "city_distribution": {},
                "language_distribution": {},
            },
            comments=[],
        )

        gender = result["analytics"]["gender"]
        self.assertAlmostEqual(gender["male"] + gender["female"], 100.0, places=1)
        self.assertEqual(gender["male"], 53.1)
        self.assertEqual(gender["female"], 46.9)

    def test_visible_gender_remains_zero_when_no_binary_signal_exists(self):
        formatter = EnhancedResponseFormatter()

        result = formatter.format_enhanced_response(
            profile_data={"username": "demo", "followers": 1000, "posts": []},
            demographics={
                "gender_distribution": {
                    "male": 0,
                    "female": 0,
                    "unknown": 100,
                },
                "age_distribution": {},
                "country_distribution": {},
                "city_distribution": {},
                "language_distribution": {},
            },
            comments=[],
        )

        self.assertEqual(result["analytics"]["gender"], {"male": 0, "female": 0})


if __name__ == "__main__":
    unittest.main()
