from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI

load_dotenv()

# ============================================================
# State
# ============================================================

class AgentState(TypedDict, total=False):
    raw_data: Dict[str, Any]
    root: Dict[str, Any]
    profile: Dict[str, Any]
    posts: List[Dict[str, Any]]
    comments: List[Dict[str, Any]]

    features: Dict[str, Any]
    llm_analytics: Dict[str, Any]
    api_response: Dict[str, Any]

    warnings: List[str]
    timings: Dict[str, float]
    started_at: float


# ============================================================
# LLM structured output schemas
# ============================================================

class RankedLocation(BaseModel):
    name: str
    percentage: float
    rank: int


class TopLocations(BaseModel):
    cities: List[RankedLocation]
    countries: List[RankedLocation]


class ViewsFollowersSplit(BaseModel):
    followerPercentage: float
    nonFollowerPercentage: float


class ViewsLLM(BaseModel):
    total: int
    accountsReached: int
    reachGrowth: str
    followers: ViewsFollowersSplit


class AgeRangeLLM(BaseModel):
    age_13_17: float = Field(alias="13-17")
    age_18_24: float = Field(alias="18-24")
    age_25_34: float = Field(alias="25-34")
    age_35_44: float = Field(alias="35-44")
    age_45_54: float = Field(alias="45-54")
    age_55_64: float = Field(alias="55-64")
    age_65_plus: float = Field(alias="65+")


class GenderLLM(BaseModel):
    male: float
    female: float


class LanguageItem(BaseModel):
    code: str
    percentage: float


class AudienceQualityLLM(BaseModel):
    score: int
    fakeFollowersPercent: float
    engagementRate: float


class LLMAnalyticsOutput(BaseModel):
    topLocations: TopLocations
    views: ViewsLLM
    ageRange: AgeRangeLLM
    gender: GenderLLM
    language: List[LanguageItem]
    audienceQuality: AudienceQualityLLM
    dataAccuracy: int
    reasoningSummary: str
    warnings: List[str]


# ============================================================
# Utility helpers
# ============================================================

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
MENTION_RE = re.compile(r"@([A-Za-z0-9_.]+)")
WORD_RE = re.compile(r"[A-Za-z\u0900-\u097F]{2,}")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]")


def now() -> float:
    return time.perf_counter()


def seconds(value: float) -> float:
    return round(float(value), 3)


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def avg(values: List[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def med(values: List[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def extract_hashtags(text: str) -> List[str]:
    return [x.lower() for x in HASHTAG_RE.findall(text or "")]


def extract_mentions(text: str) -> List[str]:
    return [x.lower() for x in MENTION_RE.findall(text or "")]


def extract_words(text: str) -> List[str]:
    return [x.lower() for x in WORD_RE.findall(text or "")]


def get_root(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    return raw_data.get("result", raw_data)


def normalize_scraped_at(value: Any) -> Optional[str]:
    if not value:
        return None

    if isinstance(value, dict) and "$date" in value:
        value = value["$date"]

    text = safe_text(value)
    if not text:
        return None

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return text


def merge_posts(root: Dict[str, Any], profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Combines profile.recent_posts and result.posts.
    If both contain the same shortcode, result.posts wins because it usually has metrics/comments.
    """
    recent_posts = profile.get("recent_posts", []) or []
    full_posts = root.get("posts", []) or []

    merged: Dict[str, Dict[str, Any]] = {}

    for idx, post in enumerate(recent_posts):
        key = safe_text(post.get("shortcode") or post.get("post_url") or f"recent_{idx}")
        merged[key] = dict(post)

    for idx, post in enumerate(full_posts):
        key = safe_text(post.get("shortcode") or post.get("post_url") or f"post_{idx}")
        previous = merged.get(key, {})
        combined = dict(previous)
        combined.update(post)
        merged[key] = combined

    return list(merged.values())


def flatten_comments(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    comments: List[Dict[str, Any]] = []

    for post in posts:
        post_shortcode = post.get("shortcode")
        for comment in post.get("comments", []) or []:
            if isinstance(comment, dict):
                item = dict(comment)
                item["post_shortcode"] = post_shortcode
                comments.append(item)

    return comments


def parse_datetime(value: Any) -> Optional[datetime]:
    text = safe_text(value)
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def build_weekday_hour_activity(posts: List[Dict[str, Any]], comments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Builds mostActiveTimes from available timestamps.
    No demographic guessing here. Only actual timestamps.
    """
    weekday_counts = {
        "Sun": 0,
        "Mon": 0,
        "Tue": 0,
        "Wed": 0,
        "Thu": 0,
        "Fri": 0,
        "Sat": 0,
    }

    hour_counts = Counter()

    weekday_map = {
        0: "Mon",
        1: "Tue",
        2: "Wed",
        3: "Thu",
        4: "Fri",
        5: "Sat",
        6: "Sun",
    }

    all_times: List[datetime] = []

    for post in posts:
        dt = parse_datetime(post.get("posted_at"))
        if dt:
            all_times.append(dt)

    for comment in comments:
        dt = parse_datetime(comment.get("posted_at"))
        if dt:
            all_times.append(dt)

    for dt in all_times:
        day = weekday_map[dt.weekday()]
        weekday_counts[day] += 1
        hour_counts[dt.hour] += 1

    total = sum(hour_counts.values())

    by_hour = []
    for hour, count in sorted(hour_counts.items()):
        percentage = round((count / total) * 100, 2) if total else 0
        by_hour.append({
            "hour": hour,
            "count": count,
            "percentage": percentage
        })

    return {
        "byDay": weekday_counts,
        "byHour": by_hour
    }


def calculate_content_type_breakdown(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counter = Counter()

    for post in posts:
        post_type = safe_text(post.get("post_type")).strip().lower()

        if post_type == "reel":
            counter["Reels"] += 1
        elif post_type == "post":
            counter["Posts"] += 1
        else:
            counter["Unknown"] += 1

    total = sum(counter.values())

    result = []
    for content_type, count in counter.most_common():
        result.append({
            "type": content_type,
            "percentage": round((count / total) * 100, 2) if total else 0,
            "count": count
        })

    return result


def distribution_list_to_dict(items: List[LanguageItem]) -> Dict[str, float]:
    return {
        item.code: round(float(item.percentage), 2)
        for item in items
    }


def age_model_to_dict(age: AgeRangeLLM) -> Dict[str, float]:
    raw = age.model_dump(by_alias=True)
    return {
        key: round(float(value), 2)
        for key, value in raw.items()
    }


def gender_model_to_dict(gender: GenderLLM) -> Dict[str, float]:
    raw = gender.model_dump()
    return {
        key: round(float(value), 2)
        for key, value in raw.items()
    }


def ranked_locations_to_dict(items: List[RankedLocation]) -> Dict[str, float]:
    return {
        item.name: round(float(item.percentage), 2)
        for item in sorted(items, key=lambda x: x.rank)
    }


def ranked_locations_to_list(items: List[RankedLocation]) -> List[Dict[str, Any]]:
    return [
        {
            "name": item.name,
            "percentage": round(float(item.percentage), 2),
            "rank": int(item.rank),
        }
        for item in sorted(items, key=lambda x: x.rank)
    ]


def compact_post_for_llm(post: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "shortcode": post.get("shortcode"),
        "post_type": post.get("post_type"),
        "posted_at": post.get("posted_at"),
        "likes_count": post.get("likes_count"),
        "views_count": post.get("views_count"),
        "comments_count_total": post.get("comments_count_total"),
        "comments_collected_count": post.get("comments_collected_count"),
        "caption": safe_text(post.get("caption") or post.get("alt"))[:1000],
    }


def compact_comment_for_llm(comment: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "username": comment.get("username"),
        "text": safe_text(comment.get("text"))[:400],
        "posted_at": comment.get("posted_at"),
        "likes_count": comment.get("likes_count"),
        "reply_count_visible": comment.get("reply_count_visible"),
        "is_verified": comment.get("is_verified"),
        "post_shortcode": comment.get("post_shortcode"),
    }


# ============================================================
# LangGraph nodes
# ============================================================

def initialize_node(state: AgentState) -> AgentState:
    state["started_at"] = now()
    state["warnings"] = []
    state["timings"] = {
        "profile_fetch_seconds": 0,
        "comments_fetch_seconds": 0,
        "feature_extraction_seconds": 0,
        "inference_seconds": 0,
        "finalize_seconds": 0,
        "total_seconds": 0,
        "stored_data_pipeline_seconds": 0,
    }
    return state


def normalize_node(state: AgentState) -> AgentState:
    raw_data = state["raw_data"]
    root = get_root(raw_data)
    profile = root.get("profile", {}) or {}
    posts = merge_posts(root, profile)
    comments = flatten_comments(posts)

    if not profile:
        state["warnings"].append("Profile object missing in input JSON.")

    if not posts:
        state["warnings"].append("No posts found in input JSON.")

    if not comments:
        state["warnings"].append("No comments found in input JSON.")

    state["root"] = root
    state["profile"] = profile
    state["posts"] = posts
    state["comments"] = comments

    return state


def feature_extraction_node(state: AgentState) -> AgentState:
    start = now()

    profile = state.get("profile", {})
    posts = state.get("posts", [])
    comments = state.get("comments", [])

    followers = safe_int(profile.get("followers_count"))

    likes_values: List[float] = []
    comment_count_values: List[float] = []
    view_values: List[float] = []

    all_captions: List[str] = []
    all_comment_texts: List[str] = []
    all_hashtags: List[str] = []
    all_mentions: List[str] = []
    all_words: List[str] = []

    emoji_comment_count = 0
    emoji_only_comment_count = 0
    unique_commenters = set()
    verified_commenter_comments = 0

    for post in posts:
        caption = safe_text(post.get("caption") or post.get("alt"))
        all_captions.append(caption)

        all_hashtags.extend(extract_hashtags(caption))
        all_mentions.extend(extract_mentions(caption))
        all_words.extend(extract_words(caption))

        if isinstance(post.get("likes_count"), (int, float)):
            likes_values.append(float(post.get("likes_count")))

        if isinstance(post.get("comments_count_total"), (int, float)):
            comment_count_values.append(float(post.get("comments_count_total")))

        if isinstance(post.get("views_count"), (int, float)):
            view_values.append(float(post.get("views_count")))

    for comment in comments:
        text = safe_text(comment.get("text"))
        all_comment_texts.append(text)

        username = safe_text(comment.get("username"))
        if username:
            unique_commenters.add(username)

        if comment.get("is_verified"):
            verified_commenter_comments += 1

        if EMOJI_RE.search(text):
            emoji_comment_count += 1

        stripped = re.sub(r"[\s\ufe0f\u200d]+", "", text)
        if stripped and not any(ch.isalnum() for ch in stripped):
            emoji_only_comment_count += 1

        all_mentions.extend(extract_mentions(text))
        all_words.extend(extract_words(text))

    total_likes = int(sum(likes_values))
    total_comments = int(sum(comment_count_values))
    total_interactions = total_likes + total_comments
    total_views_from_scrape = int(sum(view_values))

    engagement_rates = []
    if followers:
        for post in posts:
            likes = safe_int(post.get("likes_count"))
            comments_count = safe_int(post.get("comments_count_total"))
            if likes or comments_count:
                engagement_rates.append(((likes + comments_count) / followers) * 100)

    comment_text_blob = " ".join(all_comment_texts)
    caption_text_blob = " ".join(all_captions)

    devanagari_chars = sum(1 for ch in comment_text_blob + caption_text_blob if "\u0900" <= ch <= "\u097F")
    latin_chars = sum(1 for ch in comment_text_blob + caption_text_blob if ch.isascii() and ch.isalpha())
    emoji_chars = len(EMOJI_RE.findall(comment_text_blob + caption_text_blob))

    top_posts_by_likes = sorted(
        posts,
        key=lambda p: safe_int(p.get("likes_count")),
        reverse=True
    )[:12]

    top_comments_by_likes = sorted(
        comments,
        key=lambda c: safe_int(c.get("likes_count")),
        reverse=True
    )[:80]

    recent_comments = sorted(
        comments,
        key=lambda c: parse_datetime(c.get("posted_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )[:120]

    features = {
        "profile": {
            "username": profile.get("username"),
            "profile_name": profile.get("full_name"),
            "followers": followers,
            "following": safe_int(profile.get("following_count")),
            "posts_count": safe_int(profile.get("posts_count")),
            "biography": profile.get("bio"),
            "is_verified": bool(profile.get("is_verified")),
            "is_business": bool(profile.get("is_business", False)),
            "category": profile.get("category"),
            "profile_pic_url": profile.get("profile_pic_url"),
            "external_url": profile.get("external_url"),
            "highlight_titles": profile.get("highlight_titles", []) or [],
        },
        "sample_size": {
            "posts_loaded": len(posts),
            "comments_loaded": len(comments),
            "unique_commenters": len(unique_commenters),
            "verified_commenter_comments": verified_commenter_comments,
        },
        "engagement_actuals": {
            "total_likes": total_likes,
            "total_comments": total_comments,
            "total_interactions": total_interactions,
            "total_views_from_scrape": total_views_from_scrape,
            "avg_likes": round(avg(likes_values), 2),
            "median_likes": round(med(likes_values), 2),
            "avg_comments": round(avg(comment_count_values), 2),
            "median_comments": round(med(comment_count_values), 2),
            "avg_engagement_rate_by_followers": round(avg(engagement_rates), 2),
            "median_engagement_rate_by_followers": round(med(engagement_rates), 2),
        },
        "content_actuals": {
            "byContentType": calculate_content_type_breakdown(posts),
            "top_hashtags": Counter(all_hashtags).most_common(50),
            "top_mentions": Counter(all_mentions).most_common(50),
            "top_words": Counter(all_words).most_common(150),
            "emoji_comment_count": emoji_comment_count,
            "emoji_only_comment_count": emoji_only_comment_count,
            "emoji_comment_percentage": round((emoji_comment_count / len(comments)) * 100, 2) if comments else 0,
            "emoji_only_comment_percentage": round((emoji_only_comment_count / len(comments)) * 100, 2) if comments else 0,
            "script_counts": {
                "devanagari_chars": devanagari_chars,
                "latin_chars": latin_chars,
                "emoji_chars": emoji_chars,
            },
        },
        "time_actuals": {
            "mostActiveTimes": build_weekday_hour_activity(posts, comments),
        },
        "evidence_for_llm": {
            "top_posts_by_likes": [compact_post_for_llm(p) for p in top_posts_by_likes],
            "all_post_captions": [compact_post_for_llm(p) for p in posts],
            "top_comments_by_likes": [compact_comment_for_llm(c) for c in top_comments_by_likes],
            "recent_comments": [compact_comment_for_llm(c) for c in recent_comments],
        }
    }

    state["features"] = features
    state["timings"]["feature_extraction_seconds"] = seconds(now() - start)

    return state


def llm_inference_node(state: AgentState) -> AgentState:
    start = now()

    model = os.getenv("OPENAI_MODEL")
    if not model:
        raise RuntimeError("OPENAI_MODEL missing in .env")

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY missing in .env")

    features = state["features"]

    llm = ChatOpenAI(
        model=model,
        temperature=0.2,
    ).with_structured_output(LLMAnalyticsOutput)

    system_prompt = """
You are an Instagram audience analytics estimator.

You receive scraped public Instagram data:
- profile bio and metadata
- post captions
- hashtags
- comments
- likes/comments counts
- post type distribution
- timestamps
- top comments
- script and emoji signals

Your job:
Generate estimated aggregate audience analytics.

Important rules:
1. Do NOT use hardcoded demographic values.
2. Do NOT copy any fixed percentages from examples.
3. Estimate values only from the provided evidence.
4. If evidence is weak, still estimate but reduce dataAccuracy and add a warning.
5. Do NOT claim this is official Instagram Insights.
6. Do NOT infer sensitive identity attributes of individual commenters.
7. Do NOT use face/profile photo analysis.
8. Percentages should be realistic and internally consistent.
9. Country percentages should approximately sum to 100.
10. Gender percentages should approximately sum to 100.
11. Age range percentages should approximately sum to 100.
12. Language percentages should approximately sum to 100.
13. For views.total:
    - If actual views are available, use them.
    - If views are missing, estimate from likes, comments, followers, and engagement signals.
14. For accountsReached:
    - Estimate from available views/interactions/followers.
15. For fakeFollowersPercent:
    - Estimate from engagement rate, comment quality, repetition, emoji-only comments, and sample size.
16. For top cities/countries:
    - Use profile location, captions, highlights, comment language, cultural cues, and audience behavior.
17. For age/gender:
    - Use content themes, comment language, creator niche, brand categories, and engagement behavior.
"""

    user_prompt = f"""
Analyze this extracted Instagram scrape evidence and return structured analytics.

Features:
{json.dumps(features, ensure_ascii=False, indent=2)}
"""

    result: LLMAnalyticsOutput = llm.invoke([
        ("system", system_prompt),
        ("human", user_prompt),
    ])

    state["llm_analytics"] = result.model_dump(by_alias=True)
    state["timings"]["inference_seconds"] = seconds(now() - start)

    return state


def finalize_node(state: AgentState) -> AgentState:
    start = now()

    raw_data = state.get("raw_data", {})
    root = state.get("root", get_root(raw_data))
    profile = state.get("profile", {})
    posts = state.get("posts", [])
    comments = state.get("comments", [])
    features = state["features"]
    llm_raw = state["llm_analytics"]

    warnings = list(state.get("warnings", []))
    warnings.extend(llm_raw.get("warnings", []))
    warnings.append("Demographic analytics are OpenAI-estimated from scraped public data, not official Instagram Insights.")

    profile_features = features["profile"]
    engagement = features["engagement_actuals"]
    content = features["content_actuals"]
    time_actuals = features["time_actuals"]

    llm_top_locations = LLMAnalyticsOutput.model_validate(llm_raw)

    total_views = safe_int(llm_raw["views"]["total"])
    total_interactions = safe_int(engagement["total_interactions"])

    follower_pct = safe_float(llm_raw["views"]["followers"]["followerPercentage"])
    non_follower_pct = safe_float(llm_raw["views"]["followers"]["nonFollowerPercentage"])

    view_followers = round(total_views * follower_pct / 100)
    view_non_followers = max(0, total_views - view_followers)

    interaction_followers = round(total_interactions * follower_pct / 100)
    interaction_non_followers = max(0, total_interactions - interaction_followers)

    country_dict = ranked_locations_to_dict(llm_top_locations.topLocations.countries)
    city_dict = ranked_locations_to_dict(llm_top_locations.topLocations.cities)

    storage_id = (
        root.get("storage_id")
        or raw_data.get("_id", {}).get("$oid")
    )

    requested_username = (
        root.get("requested_username")
        or raw_data.get("requested_username")
        or profile.get("username")
    )

    scraped_at = (
        root.get("scraped_at")
        or raw_data.get("scraped_at")
        or raw_data.get("created_at")
    )

    max_posts_requested = (
        root.get("max_posts_requested")
        or raw_data.get("max_posts_requested")
    )

    max_comments_requested = (
        root.get("max_comments_requested")
        or raw_data.get("max_comments_requested")
    )

    state["timings"]["finalize_seconds"] = seconds(now() - start)
    total_seconds = seconds(now() - state["started_at"])
    state["timings"]["total_seconds"] = total_seconds
    state["timings"]["stored_data_pipeline_seconds"] = total_seconds

    api_response = {
        "success": True,
        "status": "success",
        "error_code": None,
        "warnings": warnings,
        "timings": {
            "profile_fetch_seconds": state["timings"]["profile_fetch_seconds"],
            "comments_fetch_seconds": state["timings"]["comments_fetch_seconds"],
            "feature_extraction_seconds": state["timings"]["feature_extraction_seconds"],
            "inference_seconds": state["timings"]["inference_seconds"],
            "finalize_seconds": state["timings"]["finalize_seconds"],
            "total_seconds": state["timings"]["total_seconds"],
            "stored_data_pipeline_seconds": state["timings"]["stored_data_pipeline_seconds"],
        },
        "retry_summary": {
            "profile": "balanced",
            "total_http_attempts": 0,
            "total_http_retries": 0,
            "rate_limit_hits": 0,
            "network_errors": 0,
            "retryable_status_hits": {},
            "operations": {},
            "last_error": None,
            "last_status": None,
        },
        "data": {
            "profile": {
                "username": profile_features["username"],
                "profile_name": profile_features["profile_name"],
                "followers": profile_features["followers"],
                "following": profile_features["following"],
                "posts_count": profile_features["posts_count"],
                "biography": profile_features["biography"],
                "is_verified": profile_features["is_verified"],
                "is_business": profile_features["is_business"],
                "profile_pic_url": profile_features["profile_pic_url"],
            },
            "analytics": {
                "topLocations": {
                    "cities": ranked_locations_to_list(llm_top_locations.topLocations.cities),
                    "countries": ranked_locations_to_list(llm_top_locations.topLocations.countries),
                },
                "views": {
                    "total": total_views,
                    "followers": {
                        "followers": view_followers,
                        "nonFollowers": view_non_followers,
                        "total": total_views,
                        "followerPercentage": follower_pct,
                        "nonFollowerPercentage": non_follower_pct,
                    },
                    "accountsReached": safe_int(llm_raw["views"]["accountsReached"]),
                    "reachGrowth": llm_raw["views"]["reachGrowth"],
                },
                "interactions": {
                    "total": total_interactions,
                    "followers": {
                        "followers": interaction_followers,
                        "nonFollowers": interaction_non_followers,
                        "total": total_interactions,
                        "followerPercentage": follower_pct,
                        "nonFollowerPercentage": non_follower_pct,
                    },
                    "byContentType": content["byContentType"],
                },
                "ageRange": age_model_to_dict(llm_top_locations.ageRange),
                "gender": gender_model_to_dict(llm_top_locations.gender),
                "language": distribution_list_to_dict(llm_top_locations.language),
                "country": country_dict,
                "city": city_dict,
                "mostActiveTimes": time_actuals["mostActiveTimes"],
                "audienceQuality": {
                    "score": safe_int(llm_raw["audienceQuality"]["score"]),
                    "fakeFollowersPercent": safe_float(llm_raw["audienceQuality"]["fakeFollowersPercent"]),
                    "engagementRate": safe_float(llm_raw["audienceQuality"]["engagementRate"]),
                },
            },
            "metrics": {
                "commentsAnalyzed": len(comments),
                "realUsersAnalyzed": features["sample_size"]["unique_commenters"],
                "dataAccuracy": safe_int(llm_raw["dataAccuracy"]),
            },
        },
        "stored_data": {
            "db": "instagpy",
            "collection": "instagram_scrapes",
            "storage_id": storage_id,
            "requested_username": requested_username,
            "scraped_at": normalize_scraped_at(scraped_at),
            "posts_loaded": len(posts),
            "posts_used_for_comments": len(posts),
            "comments_loaded": len(comments),
            "stored_comment_slots": len(comments),
            "max_posts_requested": max_posts_requested,
            "max_comments_requested": max_comments_requested,
            "used_all_posts": True,
            "used_all_stored_comments": True,
        },
        "saved_to_file": None,
        "cache": {
            "hit": False,
            "source": "stored_data",
            "age_seconds": 0,
            "ttl_seconds": None,
        },
        "_debug": {
            "llm_reasoning_summary": llm_raw.get("reasoningSummary"),
            "features_used": {
                "top_hashtags": features["content_actuals"]["top_hashtags"][:20],
                "top_words": features["content_actuals"]["top_words"][:30],
                "engagement_actuals": features["engagement_actuals"],
                "sample_size": features["sample_size"],
            }
        }
    }

    state["api_response"] = api_response
    return state


def error_response(error: Exception, raw_data: Dict[str, Any], started_at: Optional[float] = None) -> Dict[str, Any]:
    total = seconds(now() - started_at) if started_at else 0

    return {
        "success": False,
        "status": "failed",
        "error_code": "LLM_DEMOGRAPHIC_INFERENCE_FAILED",
        "warnings": [
            "No hardcoded demographic fallback was used.",
            str(error),
        ],
        "timings": {
            "profile_fetch_seconds": 0,
            "comments_fetch_seconds": 0,
            "feature_extraction_seconds": 0,
            "inference_seconds": 0,
            "finalize_seconds": 0,
            "total_seconds": total,
            "stored_data_pipeline_seconds": total,
        },
        "retry_summary": {
            "profile": "balanced",
            "total_http_attempts": 0,
            "total_http_retries": 0,
            "rate_limit_hits": 0,
            "network_errors": 0,
            "retryable_status_hits": {},
            "operations": {},
            "last_error": str(error),
            "last_status": None,
        },
        "data": None,
        "stored_data": None,
        "saved_to_file": None,
        "cache": {
            "hit": False,
            "source": "stored_data",
            "age_seconds": 0,
            "ttl_seconds": None,
        }
    }


# ============================================================
# Build LangGraph
# ============================================================

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("initialize", initialize_node)
    graph.add_node("normalize", normalize_node)
    graph.add_node("feature_extraction", feature_extraction_node)
    graph.add_node("llm_inference", llm_inference_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "normalize")
    graph.add_edge("normalize", "feature_extraction")
    graph.add_edge("feature_extraction", "llm_inference")
    graph.add_edge("llm_inference", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


# ============================================================
# Public function
# ============================================================

def analyze_instagram_json(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    started_at = now()

    try:
        app = build_graph()
        final_state = app.invoke({"raw_data": raw_data})
        return final_state["api_response"]

    except Exception as e:
        return error_response(e, raw_data, started_at)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path", help="Path to scraped Instagram JSON file")
    parser.add_argument("--output", default=None, help="Optional output JSON file")
    parser.add_argument("--hide-debug", action="store_true", help="Remove _debug from final output")
    args = parser.parse_args()

    with open(args.json_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    response = analyze_instagram_json(raw_data)

    if args.hide_debug and isinstance(response, dict):
        response.pop("_debug", None)

    if args.output:
        response["saved_to_file"] = args.output
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(response, f, ensure_ascii=False, indent=2)

    print(json.dumps(response, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()