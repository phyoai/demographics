import os
import sys
import types
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from BrightScraper.audience_analytics import AGE_BUCKETS, AudienceAnalytics
from BrightScraper.utils.feature_extractor import FeatureExtractor
from BrightScraper.utils.ml_predictor import AudiencePredictor


class _ExtractorStub:
    def extract_comment_features(self, comment):
        return dict(comment)


class _PredictorStub:
    def predict_gender(self, first_name, emoji_gender, gender_keywords):
        return {"male": 0.62, "female": 0.28, "unknown": 0.10}

    def predict_country(self, language, geotags, location_slang, hours, usernames=None):
        return {"United States": 0.63, "Canada": 0.24, "Germany": 0.13}

    def predict_city(self, **kwargs):
        return {"New York": 0.7, "Toronto": 0.3}


class _AgePredictorStub:
    def __init__(self, *, confidence=0.78):
        self.confidence = confidence

    def predict_age_for_user(self, **kwargs):
        return {"age_range": "25-34", "confidence": 0.74, "signals": {"username": {}}}

    def aggregate_age_distribution(self, user_predictions, weight_by_engagement=True):
        return {
            "age_distribution": {"18-24": 52.0, "25-34": 30.0, "45+": 18.0},
            "confidence": self.confidence,
            "total_users": len(user_predictions),
            "high_confidence_users": len(user_predictions),
            "method": "stub-age-model",
            "low_confidence_reason": "" if self.confidence >= 0.32 else "Low age confidence.",
        }


def _build_analytics(*, age_confidence=0.78):
    analytics = AudienceAnalytics.__new__(AudienceAnalytics)
    analytics.extractor = _ExtractorStub()
    analytics.predictor = _PredictorStub()
    analytics.age_predictor = _AgePredictorStub(confidence=age_confidence)
    analytics.brightdata_client = types.SimpleNamespace(retry_profile="balanced")
    analytics.use_ai = False
    analytics.ai_predictor = None

    def _scrape_profile_and_posts(self, username, retry_summary=None, deadline_at=None):
        return {
            "followers": 200000,
            "posts_count": 12,
            "posts": [
                {"url": "https://instagram.com/p/1", "location": "New York"},
                {"url": "https://instagram.com/p/2", "location": "Toronto"},
            ],
            "biography": "Based in New York",
            "avg_engagement": 0.047,
            "full_name": "Demo User",
            "is_verified": False,
            "is_business_account": False,
        }

    def _scrape_all_comments(self, posts, max_posts=8, followers=0, retry_summary=None, deadline_at=None, fast_mode=False):
        comments = []
        for idx in range(45):
            comments.append(
                {
                    "username": f"user_{idx}",
                    "text": "Great content from New York!",
                    "first_name": "Alex",
                    "emoji_gender": {"male": 1, "female": 0},
                    "gender_keywords": {"male": 1, "female": 0},
                    "location_slang": {"New York": 1},
                    "language": "en",
                    "hour": 10,
                    "full_name": "Alex Smith",
                    "profile_pic_url": "",
                    "is_bot": False,
                }
            )
        return comments, {"status": "success", "warnings": [], "posts_scraped": 2, "target_comments": 45}

    analytics.scrape_profile_and_posts = types.MethodType(_scrape_profile_and_posts, analytics)
    analytics.scrape_all_comments = types.MethodType(_scrape_all_comments, analytics)
    return analytics


class DemographicsAccuracyRevampTests(unittest.TestCase):
    def test_age_schema_uses_7_buckets_without_45_plus(self):
        analytics = _build_analytics(age_confidence=0.8)
        result = analytics.analyze_audience("demo_user", max_posts=8, fast_mode=False, deadline_seconds=30)
        age_dist = result["age_distribution"]

        self.assertEqual(set(age_dist.keys()), set(AGE_BUCKETS))
        self.assertNotIn("45+", age_dist)
        self.assertAlmostEqual(sum(age_dist.values()), 100.0, places=1)

    def test_analysis_is_deterministic_for_same_inputs(self):
        analytics = _build_analytics(age_confidence=0.8)
        first = analytics.analyze_audience("demo_user", max_posts=8, fast_mode=False, deadline_seconds=30)
        second = analytics.analyze_audience("demo_user", max_posts=8, fast_mode=False, deadline_seconds=30)

        self.assertEqual(first["gender_distribution"], second["gender_distribution"])
        self.assertEqual(first["age_distribution"], second["age_distribution"])
        self.assertEqual(first["country_distribution"], second["country_distribution"])
        self.assertEqual(first["city_distribution"], second["city_distribution"])
        self.assertEqual(first["demographics_meta"], second["demographics_meta"])

    def test_large_low_confidence_age_sample_keeps_behavioral_distribution(self):
        analytics = _build_analytics(age_confidence=0.22)
        result = analytics.analyze_audience("demo_user", max_posts=8, fast_mode=False, deadline_seconds=30)
        age_dist = result["age_distribution"]

        self.assertEqual(set(age_dist.keys()), set(AGE_BUCKETS))
        self.assertAlmostEqual(sum(age_dist.values()), 100.0, places=1)
        self.assertTrue(any(value > 0.0 for value in age_dist.values()))
        self.assertTrue(result["demographics_meta"]["age"]["lowConfidenceReason"])

    def test_feature_extractor_promotes_name_language_and_geo_signals(self):
        extractor = FeatureExtractor()

        features = extractor.extract_comment_features(
            {
                "username": "priya_sharma_99",
                "full_name": "Priya Sharma",
                "text": "Bhai main Delhi India se hoon",
            }
        )

        self.assertEqual(features["first_name"], "priya")
        self.assertEqual(features["last_name"], "sharma")
        self.assertEqual(features["name_source"], "full_name")
        self.assertGreaterEqual(features["name_confidence"], 0.9)
        self.assertEqual(features["language"], "hi")
        self.assertIn("Delhi", features["city_mentions"])
        self.assertIn("India", features["country_mentions"])
        self.assertGreater(features["gender_signal_strength"], 0.9)

    def test_gender_keywords_ignore_creator_address_terms(self):
        extractor = FeatureExtractor()

        features = extractor.extract_comment_features(
            {
                "username": "random_user_123",
                "text": "bhai bro sir this is useful",
            }
        )

        self.assertEqual(features["gender_keywords"], {"male": 0, "female": 0})

    def test_gender_predictor_does_not_default_unknown_names_to_male(self):
        predictor = AudiencePredictor()

        prediction = predictor.predict_gender(
            first_name=None,
            emoji_gender={"male": 0, "female": 0},
            gender_keywords={"male": 0, "female": 0},
        )

        self.assertGreaterEqual(prediction["unknown"], 0.95)
        self.assertLessEqual(prediction["male"], 0.05)

    def test_gender_predictor_uses_female_names_from_usernames(self):
        predictor = AudiencePredictor()
        female_names = ["priya", "vaishali", "smita", "khushburohila", "bhavanasharma"]
        male_total = 0.0
        female_total = 0.0

        for name in female_names:
            prediction = predictor.predict_gender(
                first_name=name,
                emoji_gender={"male": 0, "female": 0},
                gender_keywords={"male": 0, "female": 0},
            )
            male_total += prediction["male"]
            female_total += prediction["female"]

        self.assertGreater(female_total, male_total)

    def test_explicit_comment_locations_boost_city_and_country_distribution(self):
        analytics = _build_analytics(age_confidence=0.8)
        analytics.extractor = FeatureExtractor()

        def _location_comments(self, posts, max_posts=8, followers=0, retry_summary=None, deadline_at=None, fast_mode=False):
            comments = []
            for idx in range(40):
                comments.append(
                    {
                        "username": f"priya_sharma_{idx}",
                        "full_name": "Priya Sharma",
                        "text": "Bhai main Surat Gujarat India se hoon",
                        "is_bot": False,
                    }
                )
            return comments, {"status": "success", "warnings": [], "posts_scraped": 2, "target_comments": 40}

        analytics.scrape_all_comments = types.MethodType(_location_comments, analytics)
        result = analytics.analyze_audience("demo_user", max_posts=8, fast_mode=False, deadline_seconds=30)

        self.assertIn("Surat", result["city_distribution"])
        self.assertGreater(result["city_distribution"]["Surat"], 90.0)
        self.assertIn("India", result["country_distribution"])
        self.assertGreater(result["country_distribution"]["India"], 35.0)
        self.assertEqual(result["language_distribution"].get("hi"), 100.0)

    def test_low_confidence_gating_returns_unknown_or_empty_demographics(self):
        analytics = _build_analytics(age_confidence=0.22)

        def _low_signal_comments(self, posts, max_posts=8, followers=0, retry_summary=None, deadline_at=None, fast_mode=False):
            comments = [
                {"username": "u1", "text": "ok", "language": "unknown", "location_slang": {}, "is_bot": False},
                {"username": "u2", "text": "nice", "language": "unknown", "location_slang": {}, "is_bot": False},
            ]
            return comments, {"status": "success", "warnings": [], "posts_scraped": 1, "target_comments": 2}

        def _profile_no_geo(self, username, retry_summary=None, deadline_at=None):
            return {
                "followers": 12000,
                "posts_count": 3,
                "posts": [{"url": "https://instagram.com/p/1"}],
                "biography": "",
                "avg_engagement": 0.02,
                "full_name": "Low Signal",
                "is_verified": False,
                "is_business_account": False,
            }

        analytics.scrape_all_comments = types.MethodType(_low_signal_comments, analytics)
        analytics.scrape_profile_and_posts = types.MethodType(_profile_no_geo, analytics)

        result = analytics.analyze_audience("low_signal_user", max_posts=8, fast_mode=False, deadline_seconds=30)
        gender_dist = result["gender_distribution"]
        age_dist = result["age_distribution"]

        self.assertEqual(gender_dist, {"male": 0.0, "female": 0.0, "unknown": 100.0})
        self.assertTrue(all(value == 0.0 for value in age_dist.values()))
        self.assertEqual(result["country_distribution"], {})
        self.assertEqual(result["city_distribution"], {})
        self.assertTrue(result["demographics_meta"]["location"]["lowConfidenceReason"])


if __name__ == "__main__":
    unittest.main()
