import os
import sys
import types
import unittest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from BrightScraper.services.instagram_profile_llm_analyzer import InstagramProfileLLMAnalyzer


class _StubResponses:
    def __init__(self, output_text):
        self._output_text = output_text

    def create(self, **kwargs):
        return types.SimpleNamespace(output_text=self._output_text)


class _StubClient:
    def __init__(self, output_text):
        self.responses = _StubResponses(output_text)

    def with_options(self, **kwargs):
        return self


class InstagramProfileLLMAnalyzerTests(unittest.TestCase):
    def test_analyze_normalizes_llm_json_response(self):
        client = _StubClient(
            """
            ```json
            {
              "country": "India",
              "city": "Mumbai",
              "niche": "fashion and lifestyle",
              "category": "Fashion Creator",
              "profile_summary": "Mumbai-based fashion and lifestyle creator focused on style inspiration and brand-friendly content.",
              "confidence_notes": ["Bio mentions Mumbai", "Posts indicate fashion content"]
            }
            ```
            """
        )
        analyzer = InstagramProfileLLMAnalyzer(client=client, model="test-model")

        result = analyzer.analyze(
            {
                "requested_username": "demo_style",
                "result": {
                    "profile": {
                        "username": "demo_style",
                        "full_name": "Demo Style",
                        "bio": "Mumbai fashion creator sharing daily outfit ideas",
                        "category": "Blogger",
                        "external_links": ["https://example.com"],
                    },
                    "posts": [
                        {
                            "caption": "Street style looks from Bandra",
                            "location": "Mumbai",
                            "post_url": "https://instagram.com/p/123",
                        }
                    ],
                },
            }
        )

        self.assertEqual(result["country"], "India")
        self.assertEqual(result["city"], "Mumbai")
        self.assertEqual(result["niche"], ["fashion and lifestyle"])
        self.assertEqual(result["category"], ["Fashion Creator"])
        self.assertIn("fashion and lifestyle creator", result["profile_summary"])
        self.assertEqual(result["analysis_source"], "openai_llm_profile_analysis")
        self.assertEqual(result["analysis_model"], "test-model")

    def test_analyze_falls_back_when_client_is_unavailable(self):
        analyzer = InstagramProfileLLMAnalyzer(client=False)

        result = analyzer.analyze(
            {
                "requested_username": "demo_user",
                "result": {
                    "profile": {
                        "category": "Digital creator",
                    }
                },
            }
        )

        self.assertIsNone(result["country"])
        self.assertIsNone(result["city"])
        self.assertEqual(result["niche"], [])
        self.assertEqual(result["category"], ["Digital creator"])
        self.assertEqual(result["analysis_source"], "fallback_profile_metadata")


if __name__ == "__main__":
    unittest.main()
