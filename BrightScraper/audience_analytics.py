"""
Complete audience analytics engine.

This module now uses a shared BrightData client for pooled HTTP, bounded retries,
and consistent snapshot polling. It also supports deadline-aware partial responses.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

try:
    from .brightdata_client import (
        BrightDataBadResponseError,
        BrightDataClient,
        BrightDataRateLimitError,
        BrightDataTimeoutError,
        new_retry_summary,
    )
    from .brightdata_comments import scrape_comments_brightdata
    from .cache_manager import load_from_cache, save_to_cache
    from .utils.advanced_age_predictor import AdvancedAgePredictor
    from .utils.feature_extractor import FeatureExtractor
    from .utils.ml_predictor import AudiencePredictor
except ImportError:
    from brightdata_client import (
        BrightDataBadResponseError,
        BrightDataClient,
        BrightDataRateLimitError,
        BrightDataTimeoutError,
        new_retry_summary,
    )
    from brightdata_comments import scrape_comments_brightdata
    from cache_manager import load_from_cache, save_to_cache
    from utils.advanced_age_predictor import AdvancedAgePredictor
    from utils.feature_extractor import FeatureExtractor
    from utils.ml_predictor import AudiencePredictor


load_dotenv()

MAX_COMMENTS_PER_POST = 200
BRIGHTDATA_API_KEY = os.getenv("BRIGHTDATA_API_KEY")
BRIGHTDATA_PROFILE_DATASET = os.getenv("BRIGHTDATA_DATASET_ID")
BRIGHTDATA_BASE_URL = "https://api.brightdata.com/datasets/v3"
RETRY_PROFILE = os.getenv("BRIGHTDATA_RETRY_PROFILE", "balanced")

# Enable/disable caching
USE_CACHE = os.getenv("USE_CACHE", "True").lower() == "true"

# Enable/disable AI predictions (GPT)
USE_AI_PREDICTIONS = os.getenv("USE_AI_PREDICTIONS", "False").lower() == "true"

AGE_BUCKETS = ("13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+")
LOW_AGE_CONFIDENCE_THRESHOLD = 0.32
MIN_DEMOGRAPHIC_SIGNAL_USERS = 5
MIN_AGE_FALLBACK_SAMPLE_USERS = 30


def build_ai_predictor(use_ai: bool):
    if not use_ai:
        return None

    try:
        from .utils.ai_predictor import AIAudiencePredictor
    except ImportError:  # pragma: no cover
        try:
            from utils.ai_predictor import AIAudiencePredictor
        except ImportError:
            return None

    try:
        return AIAudiencePredictor()
    except Exception as exc:  # pragma: no cover
        print(f"Could not initialize AI predictor: {exc}")
        return None


class AnalysisDeadlineExceeded(Exception):
    """Raised when analysis exceeds the allowed deadline."""


class AudienceAnalytics:
    """Complete audience analytics system."""

    def __init__(self, use_ai: Optional[bool] = None):
        self.extractor = FeatureExtractor()
        self.predictor = AudiencePredictor()
        self.age_predictor = AdvancedAgePredictor()
        self.brightdata_client = BrightDataClient(
            api_key=BRIGHTDATA_API_KEY,
            base_url=BRIGHTDATA_BASE_URL,
            retry_profile=RETRY_PROFILE,
        )

        # Override with parameter if provided
        self.use_ai = use_ai if use_ai is not None else USE_AI_PREDICTIONS
        self.ai_predictor = build_ai_predictor(self.use_ai)
        if self.ai_predictor is None:
            self.use_ai = False

    @staticmethod
    def _assert_deadline(deadline_at: Optional[float], stage: str) -> None:
        if deadline_at is not None and time.monotonic() > deadline_at:
            raise AnalysisDeadlineExceeded(f"Deadline reached during {stage}")

    @staticmethod
    def _elapsed(started_at: float) -> float:
        return round(time.monotonic() - started_at, 3)

    @staticmethod
    def _normalize_age_distribution(
        age_distribution: Dict[str, Any],
        confidence: float,
        sample_size: int = 0,
    ) -> Dict[str, float]:
        normalized = {bucket: 0.0 for bucket in AGE_BUCKETS}
        if not isinstance(age_distribution, dict):
            return normalized
        if confidence < LOW_AGE_CONFIDENCE_THRESHOLD and sample_size < MIN_AGE_FALLBACK_SAMPLE_USERS:
            return normalized

        for raw_bucket, raw_value in age_distribution.items():
            try:
                value = max(0.0, float(raw_value))
            except (TypeError, ValueError):
                continue

            bucket = str(raw_bucket)
            if bucket in normalized:
                normalized[bucket] += value
            elif bucket == "45+":
                normalized["45-54"] += value * 0.7
                normalized["55-64"] += value * 0.2
                normalized["65+"] += value * 0.1

        total = sum(normalized.values())
        if total <= 0:
            return normalized

        normalized = {bucket: round((value / total) * 100, 1) for bucket, value in normalized.items()}
        rounding_delta = round(100.0 - sum(normalized.values()), 1)
        if rounding_delta and normalized:
            largest_bucket = max(normalized, key=normalized.get)
            normalized[largest_bucket] = round(normalized[largest_bucket] + rounding_delta, 1)
        return normalized

    @staticmethod
    def _low_location_signal_reason(
        *,
        real_user_count: int,
        geotags: List[str],
        location_slang: Counter,
        languages: List[str],
    ) -> str:
        known_languages = [lang for lang in languages if lang and lang != "unknown"]
        slang_signal_count = sum(score for score in location_slang.values() if score > 0)

        if (
            real_user_count < MIN_DEMOGRAPHIC_SIGNAL_USERS
            and not geotags
            and slang_signal_count <= 0
            and not known_languages
        ):
            return "Insufficient reliable location signals in stored comments and posts."
        return ""

    @staticmethod
    def _gender_signal_strength(comment: Dict[str, Any]) -> float:
        try:
            strength = float(comment.get("gender_signal_strength", 0.0) or 0.0)
        except (TypeError, ValueError):
            strength = 0.0

        if strength <= 0.0 and comment.get("first_name"):
            strength = 0.55
        if sum((comment.get("emoji_gender") or {}).values()) > 0:
            strength += 0.15
        if sum((comment.get("gender_keywords") or {}).values()) > 0:
            strength += 0.15
        return min(1.0, strength)

    def _aggregate_gender_distribution(self, comments: List[Dict[str, Any]]) -> tuple[Dict[str, float], Dict[str, Any]]:
        gender_scores = Counter({"male": 0.0, "female": 0.0, "unknown": 0.0})
        evidence_users = 0

        for comment in comments:
            strength = self._gender_signal_strength(comment)
            if strength < 0.25:
                gender_scores["unknown"] += 1.0
                continue

            pred = self.predictor.predict_gender(
                comment.get("first_name"),
                comment.get("emoji_gender"),
                comment.get("gender_keywords"),
            )
            gender_scores["male"] += pred.get("male", 0.0) * strength
            gender_scores["female"] += pred.get("female", 0.0) * strength
            gender_scores["unknown"] += pred.get("unknown", 0.0) * strength
            gender_scores["unknown"] += max(0.0, 1.0 - strength) * 0.5
            evidence_users += 1

        total = sum(gender_scores.values())
        if total <= 0:
            return {"male": 0.0, "female": 0.0, "unknown": 100.0}, {
                "evidenceUsers": 0,
                "lowConfidenceReason": "No reliable gender signals were available.",
            }

        distribution = {key: round((value / total) * 100, 1) for key, value in gender_scores.items()}
        low_confidence_reason = ""
        if evidence_users < MIN_DEMOGRAPHIC_SIGNAL_USERS:
            distribution = {"male": 0.0, "female": 0.0, "unknown": 100.0}
            low_confidence_reason = "Insufficient reliable user signals for gender inference."

        return distribution, {
            "evidenceUsers": evidence_users,
            "lowConfidenceReason": low_confidence_reason,
        }

    @staticmethod
    def _language_distribution_from_features(comments: List[Dict[str, Any]]) -> tuple[Dict[str, float], Dict[str, Any]]:
        language_scores = Counter()
        evidence_users = 0

        for comment in comments:
            language = comment.get("language")
            if not language or language == "unknown":
                continue
            try:
                confidence = float(comment.get("language_confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence <= 0.0:
                confidence = 0.55
            if confidence < 0.30:
                continue

            language_scores[str(language)] += min(1.0, confidence)
            evidence_users += 1

        total = sum(language_scores.values())
        if total <= 0:
            return {}, {
                "evidenceUsers": 0,
                "lowConfidenceReason": "No reliable language signals were available.",
            }

        distribution = {
            language: round((score / total) * 100, 1)
            for language, score in language_scores.most_common(5)
        }
        return distribution, {
            "evidenceUsers": evidence_users,
            "lowConfidenceReason": "",
        }

    @staticmethod
    def _merge_country_distribution(
        predicted: Dict[str, float],
        explicit_counts: Counter,
        total_users: int,
    ) -> tuple[Dict[str, float], Dict[str, Any]]:
        country_scores = Counter()

        for country, percentage in predicted.items():
            try:
                country_scores[country] += max(0.0, float(percentage)) / 100.0
            except (TypeError, ValueError):
                continue

        explicit_users = sum(explicit_counts.values())
        if total_users > 0:
            for country, count in explicit_counts.items():
                explicit_ratio = count / total_users
                country_scores[country] += min(0.75, explicit_ratio * 3.0)

        total = sum(country_scores.values())
        if total <= 0:
            return {}, {
                "explicitUsers": explicit_users,
                "lowConfidenceReason": "No reliable country signals were available.",
            }

        distribution = {
            country: round((score / total) * 100, 1)
            for country, score in country_scores.most_common(5)
        }
        return distribution, {
            "explicitUsers": explicit_users,
            "lowConfidenceReason": "",
        }

    @staticmethod
    def _merge_city_distribution(
        predicted: Dict[str, float],
        explicit_counts: Counter,
        total_users: int,
    ) -> tuple[Dict[str, float], Dict[str, Any]]:
        city_scores = Counter()

        for city, percentage in predicted.items():
            try:
                city_scores[city] = max(city_scores[city], max(0.0, float(percentage)))
            except (TypeError, ValueError):
                continue

        explicit_users = sum(explicit_counts.values())
        if total_users > 0:
            for city, count in explicit_counts.items():
                explicit_percentage = round((count / total_users) * 100, 1)
                city_scores[city] = max(city_scores[city], explicit_percentage)

        distribution = {
            city: round(score, 1)
            for city, score in city_scores.most_common(5)
            if score >= 0.5
        }
        low_confidence_reason = "" if distribution else "No reliable city signals were available."
        return distribution, {
            "explicitUsers": explicit_users,
            "lowConfidenceReason": low_confidence_reason,
        }

    @staticmethod
    def _flag_repeated_long_comment_bots(comments: List[Dict[str, Any]]) -> None:
        text_counts = Counter(
            str(comment.get("normalized_text") or comment.get("text") or "").strip().lower()
            for comment in comments
        )
        for comment in comments:
            normalized = str(comment.get("normalized_text") or comment.get("text") or "").strip().lower()
            if len(normalized) >= 40 and text_counts.get(normalized, 0) >= 3:
                comment["is_bot"] = True
                comment["spam_score"] = max(int(comment.get("spam_score") or 0), 3)

    def get_snapshot_data(
        self,
        snapshot_id: str,
        *,
        retry_summary: Optional[Dict[str, Any]] = None,
        deadline_at: Optional[float] = None,
        max_retries: int = 20,
        retry_delay: float = 2.0,
        operation: str = "profile_snapshot_poll",
    ) -> List[Dict[str, Any]]:
        """Fetch snapshot data using shared BrightData client."""
        print(f"  Waiting for snapshot {snapshot_id}...")
        data = self.brightdata_client.fetch_snapshot_data(
            snapshot_id,
            summary=retry_summary,
            deadline_at=deadline_at,
            poll_retries=max_retries,
            poll_delay=retry_delay,
            operation=operation,
        )
        if isinstance(data, list):
            print(f"  Snapshot ready. Got {len(data)} records")
            return data
        return []

    def scrape_profile_and_posts(
        self,
        username: str,
        *,
        retry_summary: Optional[Dict[str, Any]] = None,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Scrape profile + posts (with optional cache)."""
        self._assert_deadline(deadline_at, "profile_fetch_start")

        if USE_CACHE:
            cached_data = load_from_cache("profile", username)
            if cached_data:
                return cached_data

        if not BRIGHTDATA_PROFILE_DATASET:
            raise BrightDataBadResponseError("BrightData profile dataset id is not configured")

        tracker = retry_summary if retry_summary is not None else new_retry_summary(profile=self.brightdata_client.retry_profile)

        print(f"Fetching from API: profile for {username}")
        snapshot_id = self.brightdata_client.trigger_snapshot(
            dataset_id=BRIGHTDATA_PROFILE_DATASET,
            payload=[{"user_name": username}],
            include_errors=True,
            extra_query={"type": "discover_new", "discover_by": "user_name"},
            summary=tracker,
            deadline_at=deadline_at,
            operation="profile_trigger",
        )

        print(f"Profile Snapshot: {snapshot_id}")
        data = self.get_snapshot_data(
            snapshot_id,
            retry_summary=tracker,
            deadline_at=deadline_at,
            max_retries=20,
            retry_delay=2.0,
            operation="profile_snapshot_poll",
        )

        if not data:
            raise ValueError(f"No profile data found for @{username}")

        profile_data = data[0]

        # BrightData sometimes returns row-level errors in data payload.
        if "error" in profile_data or "error_code" in profile_data:
            error_msg = str(profile_data.get("error", "Unknown error"))
            error_l = error_msg.lower()
            if "not exist" in error_l or "not found" in error_l:
                raise ValueError(f"Account @{username} does not exist")
            if "private" in error_l:
                raise ValueError(f"Account @{username} is private")
            if "rate limit" in error_l:
                raise BrightDataRateLimitError(f"Rate limit exceeded while scraping @{username}", retry_summary=tracker)
            raise BrightDataBadResponseError(
                f"Error scraping @{username}: {error_msg} (code: {profile_data.get('error_code', 'unknown')})",
                retry_summary=tracker,
            )

        if USE_CACHE:
            save_to_cache("profile", username, profile_data)

        return profile_data

    def scrape_post_comments(
        self,
        post_url: str,
        *,
        max_comments: Optional[int] = None,
        retry_summary: Optional[Dict[str, Any]] = None,
        deadline_at: Optional[float] = None,
        fast_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        """Scrape comments from a single post using BrightData (with cache)."""
        self._assert_deadline(deadline_at, "comments_fetch_start")

        if USE_CACHE:
            cached_data = load_from_cache("comments", post_url)
            if cached_data:
                return cached_data

        print(f"Fetching comments for {post_url}")

        comments = scrape_comments_brightdata(
            post_url,
            max_comments=max_comments or MAX_COMMENTS_PER_POST,
            client=self.brightdata_client,
            retry_summary=retry_summary,
            deadline_at=deadline_at,
            max_poll_retries=10 if fast_mode else None,
            poll_retry_delay=1.5 if fast_mode else None,
        )

        if USE_CACHE:
            save_to_cache("comments", post_url, comments)

        return comments

    @staticmethod
    def _comment_sampling_targets(followers: int, fast_mode: bool) -> Tuple[int, int]:
        """Return (target_comments, comments_per_post)."""
        if fast_mode:
            if followers >= 1_000_000:
                return 260, 100
            if followers >= 500_000:
                return 220, 80
            if followers >= 100_000:
                return 180, 60
            return 120, 40

        if followers >= 1_000_000:
            return 1000, 200
        if followers >= 500_000:
            return 800, 180
        if followers >= 100_000:
            return 400, 120
        return 250, 80

    def scrape_all_comments(
        self,
        posts: List[Dict[str, Any]],
        *,
        max_posts: int = 6,
        followers: int = 0,
        retry_summary: Optional[Dict[str, Any]] = None,
        deadline_at: Optional[float] = None,
        fast_mode: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Scrape comments from multiple posts and honor max_posts + deadline."""
        target_comments, comments_per_post = self._comment_sampling_targets(followers, fast_mode)

        posts_to_scan = posts[: max(1, int(max_posts))]
        all_comments: List[Dict[str, Any]] = []
        warnings: List[str] = []
        posts_scraped = 0
        deadline_hit = False

        print(
            f"Comment sampling target={target_comments}, "
            f"comments_per_post={comments_per_post}, posts_to_scan={len(posts_to_scan)}"
        )

        for index, post in enumerate(posts_to_scan, start=1):
            if deadline_at is not None and time.monotonic() > deadline_at:
                deadline_hit = True
                warnings.append("Deadline reached while scraping comments; using partial comment sample.")
                break

            if len(all_comments) >= target_comments:
                break

            post_url = post.get("url")
            if not post_url:
                continue

            posts_scraped += 1
            remaining = target_comments - len(all_comments)
            comments_to_fetch = min(comments_per_post, remaining)

            print(
                f"[{index}/{len(posts_to_scan)}] Scraping comments: {post_url} "
                f"(need {comments_to_fetch}, total_collected={len(all_comments)})"
            )

            try:
                comments = self.scrape_post_comments(
                    post_url,
                    max_comments=comments_to_fetch,
                    retry_summary=retry_summary,
                    deadline_at=deadline_at,
                    fast_mode=fast_mode,
                )
            except AnalysisDeadlineExceeded:
                deadline_hit = True
                warnings.append("Deadline reached while scraping comments; using collected comments so far.")
                break

            valid_comments = [c for c in comments if (c.get("text") or "").strip()]
            all_comments.extend(valid_comments)

            if index < len(posts_to_scan):
                # Keep short pacing delay to avoid bursty upstream calls.
                time.sleep(0.15 if fast_mode else 0.5)

        status = "partial" if deadline_hit else "success"
        meta = {
            "status": status,
            "warnings": warnings,
            "posts_scraped": posts_scraped,
            "posts_considered": len(posts_to_scan),
            "target_comments": target_comments,
            "collected_comments": len(all_comments),
        }
        return all_comments, meta

    def analyze_audience(
        self,
        username: str,
        max_posts: int = 6,
        *,
        deadline_seconds: Optional[float] = None,
        fast_mode: bool = False,
        limit_age_source: bool = True,
    ) -> Dict[str, Any]:
        """
        Complete audience analysis.

        Returns a Modash-style analytics payload plus operational metadata:
        - analysis_status: success|partial
        - warnings
        - timings
        - retry_summary
        """
        started_at = time.monotonic()
        timings: Dict[str, float] = {}
        warnings: List[str] = []
        analysis_status = "success"
        retry_summary = new_retry_summary(profile=self.brightdata_client.retry_profile)

        deadline_at: Optional[float] = None
        if deadline_seconds and deadline_seconds > 0:
            deadline_at = started_at + float(deadline_seconds)

        print("=" * 60)
        print(f"ANALYZING: @{username}")
        print("=" * 60)

        # --- Step 1: profile + posts ---
        step_started = time.monotonic()
        profile = self.scrape_profile_and_posts(
            username,
            retry_summary=retry_summary,
            deadline_at=deadline_at,
        )
        timings["profile_fetch_seconds"] = self._elapsed(step_started)

        if not profile:
            raise ValueError(f"Profile data not found for @{username}")

        followers = profile.get("followers_count", 0) or profile.get("followers", 0) or 0
        posts_data = profile.get("posts", [])
        posts_count = profile.get("posts_count", 0) or len(posts_data) or 0

        if followers == 0 and posts_count == 0:
            raise ValueError(f"Account @{username} appears to not exist or is private (0 followers, 0 posts)")

        posts = posts_data
        print(f"Profile found: followers={followers:,}, posts={posts_count}")

        # --- Step 2: comments ---
        step_started = time.monotonic()
        comments, comment_meta = self.scrape_all_comments(
            posts,
            max_posts=max_posts,
            followers=followers,
            retry_summary=retry_summary,
            deadline_at=deadline_at,
            fast_mode=fast_mode,
        )
        timings["comments_fetch_seconds"] = self._elapsed(step_started)
        warnings.extend(comment_meta.get("warnings", []))
        if comment_meta.get("status") == "partial":
            analysis_status = "partial"

        # --- Step 3: feature extraction ---
        step_started = time.monotonic()
        extracted_comments = [self.extractor.extract_comment_features(comment) for comment in comments]
        self._flag_repeated_long_comment_bots(extracted_comments)
        timings["feature_extraction_seconds"] = self._elapsed(step_started)

        if deadline_at is not None and time.monotonic() > deadline_at:
            warnings.append("Deadline reached after feature extraction; using partial demographic inference.")
            analysis_status = "partial"

        # --- Step 4: inference ---
        step_started = time.monotonic()
        real_comments = [c for c in extracted_comments if not c.get("is_bot", False)]

        gender_dist: Dict[str, float] = {"male": 45.0, "female": 52.0, "unknown": 3.0}
        age_dist: Dict[str, float] = {"13-17": 5, "18-24": 35, "25-34": 40, "35-44": 12, "45-54": 5, "55-64": 2, "65+": 1}
        country_dist: Dict[str, float] = {"India": 70.0}
        city_dist: Dict[str, float] = {}
        language_dist: Dict[str, float] = {"en": 60, "hi": 30, "ml": 10}
        languages: List[str] = []
        geotags: List[str] = []
        all_slang: Counter = Counter()
        all_usernames: List[str] = []
        all_full_names: List[str] = []
        explicit_city_counts: Counter = Counter()
        explicit_country_counts: Counter = Counter()
        gender_meta: Dict[str, Any] = {
            "evidenceUsers": 0,
            "lowConfidenceReason": "",
        }
        language_meta: Dict[str, Any] = {
            "evidenceUsers": 0,
            "lowConfidenceReason": "",
        }
        country_meta: Dict[str, Any] = {
            "explicitUsers": 0,
            "lowConfidenceReason": "",
        }
        city_meta: Dict[str, Any] = {
            "explicitUsers": 0,
            "lowConfidenceReason": "",
        }
        age_metadata: Dict[str, Any] = {
            "confidence": 0.25,
            "method": "fallback demographics (no user predictions)",
            "total_users": 0,
            "high_confidence_users": 0,
            "low_confidence_reason": "No reliable age signals were available.",
        }

        if self.use_ai and self.ai_predictor.client and real_comments:
            commenters_data = [
                {
                    "username": c.get("username", "unknown"),
                    "comment": c.get("text", "")[:200],
                }
                for c in real_comments
            ]
            ai_result = self.ai_predictor.analyze_all_commenters(commenters_data)
            if ai_result:
                gender_dist = ai_result.get("gender_distribution", gender_dist)
                age_dist = ai_result.get("age_distribution", age_dist)
                country_dist = ai_result.get("country_distribution", country_dist)
                city_dist = ai_result.get("city_distribution", city_dist)
                age_metadata = {
                    "confidence": ai_result.get("age_confidence", age_metadata["confidence"]),
                    "method": ai_result.get("age_method", "ai-inference"),
                    "total_users": ai_result.get("total_users", len(real_comments)),
                    "high_confidence_users": ai_result.get("high_confidence_users", len(real_comments)),
                }
            else:
                self.use_ai = False

        if not self.use_ai:
            gender_dist, gender_meta = self._aggregate_gender_distribution(real_comments)

            languages = [c.get("language") for c in real_comments if c.get("language")]
            hours = [c.get("hour") for c in real_comments if c.get("hour") is not None]
            all_slang = Counter()
            explicit_city_counts = Counter()
            explicit_country_counts = Counter()
            for c in real_comments:
                for loc, score in c.get("location_slang", {}).items():
                    all_slang[loc] += score
                for city, score in (c.get("city_mentions") or {}).items():
                    explicit_city_counts[city] += score
                    all_slang[city] += score * 3
                for country, score in (c.get("country_mentions") or {}).items():
                    explicit_country_counts[country] += score

            geotags = []
            for post in posts[: max(1, max_posts)]:
                location = post.get("location")
                if location:
                    if isinstance(location, str):
                        geotags.append(location)
                    elif isinstance(location, dict):
                        geotags.append(location.get("name") or location.get("city") or location.get("address") or str(location))
                    else:
                        geotags.append(str(location))

            if languages:
                language_counts = Counter(languages)
                dominant_language = language_counts.most_common(1)[0][0]
            else:
                dominant_language = "en"

            all_usernames = [c.get("username") for c in real_comments if c.get("username")]

            try:
                country_pred = self.predictor.predict_country(
                    language=dominant_language,
                    geotags=geotags,
                    location_slang=dict(all_slang),
                    hours=hours,
                    usernames=all_usernames,
                )
                predicted_country_dist = {k: round(v * 100, 1) for k, v in list(country_pred.items())[:5]}
                country_dist, country_meta = self._merge_country_distribution(
                    predicted_country_dist,
                    explicit_country_counts,
                    len(real_comments),
                )
            except Exception:
                country_dist = country_dist

            all_full_names = [
                c.get("full_name")
                for c in real_comments
                if c.get("full_name") and str(c.get("full_name")).strip()
            ]

            try:
                city_pred = self.predictor.predict_city(
                    geotags=geotags,
                    location_slang=dict(all_slang),
                    hours=hours,
                    bio_text=profile.get("biography", ""),
                    usernames=all_usernames,
                    full_names=all_full_names,
                    extracted_comments=real_comments,
                )
                city_dist, city_meta = self._merge_city_distribution(
                    city_pred if isinstance(city_pred, dict) else {},
                    explicit_city_counts,
                    len(real_comments),
                )
            except Exception:
                city_dist = {}

            # In fast mode, avoid expensive face detection by skipping profile images.
            user_age_predictions = []
            age_source = extracted_comments
            if limit_age_source and fast_mode and len(age_source) > 150:
                age_source = age_source[:150]

            for comment in age_source:
                if comment.get("is_bot", False):
                    continue

                prediction = self.age_predictor.predict_age_for_user(
                    username=comment.get("username", ""),
                    comment_text=comment.get("text", ""),
                    full_name=comment.get("full_name"),
                    profile_pic_url=None if fast_mode else comment.get("profile_pic_url"),
                    comment_likes=comment.get("likes", 0),
                    emoji_density=comment.get("emoji_density", 0.0),
                )
                user_age_predictions.append(prediction)

            age_result = self.age_predictor.aggregate_age_distribution(
                user_age_predictions,
                weight_by_engagement=True,
            )

            age_dist = age_result.get("age_distribution", age_dist)
            age_metadata = {
                "confidence": age_result.get("confidence", 0.0),
                "method": age_result.get("method", "multi-signal inference"),
                "total_users": age_result.get("total_users", len(user_age_predictions)),
                "high_confidence_users": age_result.get("high_confidence_users", 0),
                "low_confidence_reason": age_result.get("low_confidence_reason", ""),
            }

            # Language distribution from extracted comments
            language_dist, language_meta = self._language_distribution_from_features(real_comments)

        timings["inference_seconds"] = self._elapsed(step_started)

        real_user_count = len([c for c in extracted_comments if not c.get("is_bot")])
        try:
            age_confidence = float(age_metadata.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            age_confidence = 0.0
        try:
            age_prediction_count = int(age_metadata.get("total_users", 0) or 0)
        except (TypeError, ValueError):
            age_prediction_count = 0
        age_sample_size = max(age_prediction_count, real_user_count)
        age_low_confidence_reason = str(age_metadata.get("low_confidence_reason") or "")
        if age_confidence < LOW_AGE_CONFIDENCE_THRESHOLD and not age_low_confidence_reason:
            if age_sample_size >= MIN_AGE_FALLBACK_SAMPLE_USERS:
                age_low_confidence_reason = (
                    "Age confidence below reliability threshold; returned large-sample "
                    "behavioral distribution instead of empty buckets."
                )
            else:
                age_low_confidence_reason = "Age confidence below reliability threshold."

        age_dist = self._normalize_age_distribution(
            age_dist,
            age_confidence,
            sample_size=age_sample_size,
        )

        location_low_confidence_reason = self._low_location_signal_reason(
            real_user_count=real_user_count,
            geotags=geotags,
            location_slang=all_slang,
            languages=languages,
        )
        gender_low_confidence_reason = gender_meta.get("lowConfidenceReason", "")
        if location_low_confidence_reason and real_user_count < MIN_DEMOGRAPHIC_SIGNAL_USERS:
            gender_dist = {"male": 0.0, "female": 0.0, "unknown": 100.0}
            country_dist = {}
            city_dist = {}
            gender_low_confidence_reason = "Insufficient reliable user signals for gender inference."

        # --- Step 5: scoring + output ---
        step_started = time.monotonic()
        bot_count = sum(1 for c in extracted_comments if c.get("is_bot", False))
        fake_follower_percent = round((bot_count / len(extracted_comments)) * 100, 1) if extracted_comments else 0

        engagement_rate = profile.get("avg_engagement", 0) * 100
        normalized_engagement = min(engagement_rate, 10)
        aq_score = round(
            (1 - (fake_follower_percent / 100)) * 40
            + (normalized_engagement / 10) * 40
            + 20,
            1,
        )
        aq_score = min(max(aq_score, 0), 100)

        profile_pic_url = (
            profile.get("profile_pic_url")
            or profile.get("profile_image")
            or profile.get("profile_image_link")
            or profile.get("profile_picture")
            or ""
        )

        result = {
            "username": username,
            "profile_name": profile.get("full_name", profile.get("profile_name", "")),
            "profile_pic_url": profile_pic_url,
            "followers": profile.get("followers", 0),
            "following": profile.get("following", 0),
            "posts_count": profile.get("posts_count", 0),
            "biography": profile.get("biography", ""),
            "is_verified": profile.get("is_verified", False),
            "is_business": profile.get("is_business_account", False),
            "avg_engagement": round(profile.get("avg_engagement", 0) * 100, 2),
            "gender_distribution": gender_dist,
            "age_distribution": age_dist,
            "age_confidence": age_metadata.get("confidence", 0.0),
            "age_method": age_metadata.get("method", "multi-signal inference"),
            "country_distribution": country_dist,
            "city_distribution": city_dist,
            "language_distribution": language_dist,
            "demographics_meta": {
                "sample": {
                    "commentsAnalyzed": len(comments),
                    "realUsersAnalyzed": real_user_count,
                    "postsAnalyzed": len(posts[: max(1, max_posts)]),
                },
                "age": {
                    "confidence": age_confidence,
                    "method": age_metadata.get("method", "multi-signal inference"),
                    "totalUsers": age_metadata.get("total_users", 0),
                    "highConfidenceUsers": age_metadata.get("high_confidence_users", 0),
                    "lowConfidenceReason": age_low_confidence_reason,
                },
                "gender": {
                    "lowConfidenceReason": gender_low_confidence_reason,
                    "evidenceUsers": gender_meta.get("evidenceUsers", 0),
                },
                "language": {
                    "lowConfidenceReason": language_meta.get("lowConfidenceReason", ""),
                    "evidenceUsers": language_meta.get("evidenceUsers", 0),
                },
                "country": {
                    "lowConfidenceReason": country_meta.get("lowConfidenceReason", ""),
                    "explicitUsers": country_meta.get("explicitUsers", 0),
                },
                "location": {
                    "lowConfidenceReason": location_low_confidence_reason or city_meta.get("lowConfidenceReason", ""),
                    "geotagCount": len(geotags),
                    "locationSlangSignalCount": sum(
                        score for score in all_slang.values() if score > 0
                    ),
                    "explicitCityUsers": city_meta.get("explicitUsers", 0),
                },
            },
            "audience_quality_score": aq_score,
            "fake_followers_percent": fake_follower_percent,
            "total_comments_analyzed": len(comments),
            "real_users_analyzed": real_user_count,
            "comments": comments,
            "posts": posts[: max(1, max_posts)],
            "analysis_status": analysis_status,
            "warnings": warnings,
            "timings": timings,
            "retry_summary": retry_summary,
        }

        timings["finalize_seconds"] = self._elapsed(step_started)
        timings["total_seconds"] = self._elapsed(started_at)

        if deadline_at is not None and time.monotonic() > deadline_at:
            analysis_status = "partial"
            result["analysis_status"] = "partial"
            result["warnings"] = warnings + ["Deadline reached before analysis completed; returned partial data."]

        return result


if __name__ == "__main__":
    analytics = AudienceAnalytics()
    output = analytics.analyze_audience("codebitabhi", max_posts=3, deadline_seconds=30, fast_mode=True)
    print(f"Analysis status: {output.get('analysis_status')}")
    print(f"Followers: {output.get('followers')}")
    print(f"Comments analyzed: {output.get('total_comments_analyzed')}")
