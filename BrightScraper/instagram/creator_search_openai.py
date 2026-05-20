# creator_search_openai_tool.py

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http.models import FieldCondition, Filter, MatchAny, MatchValue, Range
from qdrant_client.models import Document

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION_NAME = "instagram_creator_profiles"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_OPENAI_MODEL = os.getenv("CREATOR_SEARCH_OPENAI_MODEL", "gpt-4.1-mini")

COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

COUNTRY_ALIASES = {
    "america": "United States",
    "australia": "Australia",
    "canada": "Canada",
    "india": "India",
    "indian": "India",
    "singapore": "Singapore",
    "uae": "United Arab Emirates",
    "uk": "United Kingdom",
    "united arab emirates": "United Arab Emirates",
    "united kingdom": "United Kingdom",
    "united states": "United States",
    "usa": "United States",
}

LOCATION_STOPWORDS = {
    "accounts",
    "blogger",
    "bloggers",
    "content",
    "creator",
    "creators",
    "followers",
    "following",
    "influencer",
    "influencers",
    "instagram",
    "posts",
    "profile",
    "profiles",
    "users",
    "with",
}


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _build_openai_client() -> OpenAI | None:
    api_key = _normalize_whitespace(os.getenv("OPENAI_API_KEY", ""))
    if not api_key:
        return None

    try:
        return OpenAI(api_key=api_key, max_retries=0)
    except Exception as exc:
        logger.warning("Creator search OpenAI client setup failed: %s", exc)
        return None


def _extract_limit(user_message: str, default: int = 10) -> int:
    prompt = _normalize_whitespace(user_message).lower()
    if not prompt:
        return default

    digit_patterns = (
        r"\b(?P<count>\d{1,3})(?!\s*[km]\b)\s+(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b",
        r"\b(?:need|want|find|get|show|give)\s+(?P<count>\d{1,3})(?!\s*[km]\b)\b",
        r"^(?P<count>\d{1,3})(?!\s*[km]\b)\b",
    )

    for pattern in digit_patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group("count")))

    word_pattern = (
        r"\b(?P<count>one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
        r"eighteen|nineteen|twenty)\s+"
        r"(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b"
    )
    match = re.search(word_pattern, prompt, flags=re.IGNORECASE)
    if match:
        return COUNT_WORDS.get(match.group("count"), default)

    return default


def _strip_limit_request(user_message: str) -> str:
    cleaned = _normalize_whitespace(user_message)
    if not cleaned:
        return ""

    patterns = (
        r"\b(?:need|want|find|get|show|give)\s+\d{1,3}(?!\s*[km]\b)\s+",
        r"\b\d{1,3}(?!\s*[km]\b)\s+(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b",
        r"^(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s+(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b",
        r"^\d{1,3}(?!\s*[km]\b)\s+",
        r"^(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s+",
    )

    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    return _normalize_whitespace(cleaned.strip(" ,"))


def _parse_compact_number(value: str | None) -> int | None:
    if not value:
        return None

    match = re.fullmatch(r"(?P<number>\d+(?:\.\d+)?)(?P<suffix>[km])?", value.strip().lower())
    if not match:
        return None

    number = float(match.group("number"))
    suffix = match.group("suffix")
    multiplier = 1

    if suffix == "k":
        multiplier = 1_000
    elif suffix == "m":
        multiplier = 1_000_000

    return int(number * multiplier)


def _extract_follower_bounds(user_message: str) -> tuple[int | None, int | None]:
    prompt = _normalize_whitespace(user_message).lower()
    if not prompt:
        return None, None

    between_match = re.search(
        r"\bbetween\s+(?P<min>\d+(?:\.\d+)?[km]?)\s+and\s+(?P<max>\d+(?:\.\d+)?[km]?)\s+followers?\b",
        prompt,
        flags=re.IGNORECASE,
    )
    if between_match:
        return (
            _parse_compact_number(between_match.group("min")),
            _parse_compact_number(between_match.group("max")),
        )

    min_patterns = (
        r"\b(?:at least|minimum|min|over|above|more than)\s+(?P<min>\d+(?:\.\d+)?[km]?)\s+followers?\b",
        r"\bwith\s+(?P<min>\d+(?:\.\d+)?[km]?)\+?\s+followers?\b",
    )
    max_patterns = (
        r"\b(?:at most|maximum|max|under|below|less than)\s+(?P<max>\d+(?:\.\d+)?[km]?)\s+followers?\b",
    )

    min_followers = None
    max_followers = None

    for pattern in min_patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            min_followers = _parse_compact_number(match.group("min"))
            break

    for pattern in max_patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            max_followers = _parse_compact_number(match.group("max"))
            break

    if min_followers is not None and max_followers is not None and min_followers > max_followers:
        min_followers, max_followers = max_followers, min_followers

    return min_followers, max_followers


def _normalize_country_name(value: str | None) -> str | None:
    if not value:
        return None

    normalized = _normalize_whitespace(value).strip(" ,.")
    if not normalized:
        return None

    return COUNTRY_ALIASES.get(normalized.lower(), normalized.title())


def _normalize_city_name(value: str | None) -> str | None:
    if not value:
        return None

    normalized = _normalize_whitespace(value).strip(" ,.")
    if not normalized or normalized.lower() in LOCATION_STOPWORDS:
        return None

    if normalized.lower() in COUNTRY_ALIASES:
        return None

    return normalized.title()


def _extract_location_filters(user_message: str) -> tuple[str | None, str | None]:
    prompt = _normalize_whitespace(user_message)
    if not prompt:
        return None, None

    match = re.search(
        r"\b(?:in|from|based in|located in|out of|near)\s+([A-Za-z][A-Za-z\s,.'-]{1,60})",
        prompt,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None

    fragment = re.split(
        (
            r"\b(?:with|having|under|below|over|above|at least|at most|followers?|"
            r"following|posts?|creator|creators|influencer|influencers|blogger|bloggers)\b"
        ),
        match.group(1),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,.")

    if not fragment:
        return None, None

    parts = [part.strip() for part in fragment.split(",") if part.strip()]
    if len(parts) >= 2:
        return _normalize_city_name(parts[0]), _normalize_country_name(parts[-1])

    country = _normalize_country_name(fragment)
    if country and country.lower() != fragment.lower():
        return None, country

    direct_country = COUNTRY_ALIASES.get(fragment.lower())
    if direct_country:
        return None, direct_country

    return _normalize_city_name(fragment), None


def _build_fallback_search_arguments(user_message: str) -> dict[str, Any]:
    normalized_message = _normalize_whitespace(user_message)
    cleaned_query = _strip_limit_request(normalized_message)
    min_followers, max_followers = _extract_follower_bounds(normalized_message)
    city, country = _extract_location_filters(normalized_message)

    if not cleaned_query:
        cleaned_query = normalized_message

    return {
        "query": cleaned_query,
        "country": country,
        "city": city,
        "niches": None,
        "min_followers": min_followers,
        "max_followers": max_followers,
        "limit": _extract_limit(normalized_message),
    }


def _normalize_tool_arguments(
    arguments: dict[str, Any],
    fallback_arguments: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(fallback_arguments)

    query = _normalize_whitespace(str(arguments.get("query", "")))
    if query:
        normalized["query"] = query

    for key in ("country", "city"):
        value = arguments.get(key)
        if isinstance(value, str):
            value = _normalize_whitespace(value)
            normalized[key] = value or None

    niches = arguments.get("niches")
    if isinstance(niches, list):
        cleaned_niches = [
            _normalize_whitespace(str(item))
            for item in niches
            if _normalize_whitespace(str(item))
        ]
        normalized["niches"] = cleaned_niches or None

    for key in ("min_followers", "max_followers", "limit"):
        value = arguments.get(key)
        if value is None:
            continue

        try:
            numeric_value = int(value)
        except (TypeError, ValueError):
            continue

        if key == "limit":
            normalized[key] = max(1, numeric_value)
        elif numeric_value >= 0:
            normalized[key] = numeric_value

    return normalized


def build_qdrant_client() -> QdrantClient:
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    if not qdrant_url or not qdrant_api_key:
        raise ValueError("Set QDRANT_URL and QDRANT_API_KEY in .env")

    return QdrantClient(
        url=qdrant_url,
        api_key=qdrant_api_key,
        cloud_inference=True,
    )


def build_filter(
    country: str | None = None,
    city: str | None = None,
    niches: list[str] | None = None,
    min_followers: int | None = None,
    max_followers: int | None = None,
) -> Filter | None:
    conditions: list[FieldCondition] = []

    if country:
        conditions.append(
            FieldCondition(
                key="country",
                match=MatchValue(value=country),
            )
        )

    if city:
        conditions.append(
            FieldCondition(
                key="city",
                match=MatchValue(value=city),
            )
        )

    if niches:
        conditions.append(
            FieldCondition(
                key="niche",
                match=MatchAny(any=niches),
            )
        )

    if min_followers is not None or max_followers is not None:
        conditions.append(
            FieldCondition(
                key="followers",
                range=Range(
                    gte=min_followers,
                    lte=max_followers,
                ),
            )
        )

    if not conditions:
        return None

    return Filter(must=conditions)


def search_creator_profiles(
    query: str,
    country: str | None = None,
    city: str | None = None,
    niches: list[str] | None = None,
    min_followers: int | None = None,
    max_followers: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    client = build_qdrant_client()

    query_filter = build_filter(
        country=country,
        city=city,
        niches=niches or [],
        min_followers=min_followers,
        max_followers=max_followers,
    )

    results = client.query_points(
        collection_name=os.getenv("QDRANT_COLLECTION", DEFAULT_COLLECTION_NAME),
        query=Document(
            text=query,
            model=DEFAULT_EMBEDDING_MODEL,
        ),
        query_filter=query_filter,
        with_payload=True,
        limit=max(1, limit),
    )

    final_data = []

    for point in results.points:
        payload = point.payload or {}

        final_data.append(
            {
                "username": payload.get("username"),
                "full_name": payload.get("full_name"),
                "profile_url": payload.get("profile_url"),
                "followers": payload.get("followers"),
                "following": payload.get("following"),
                "posts": payload.get("posts"),
                "location": payload.get("location"),
                "country": payload.get("country"),
                "city": payload.get("city"),
                "niche": payload.get("niche"),
                "profile_summary": payload.get("profile_summary"),
                "score": point.score,
            }
        )

    return final_data


tools = [
    {
        "type": "function",
        "name": "search_creator_profiles",
        "description": "Search Instagram creator profiles from Qdrant using semantic query and filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, for example: food creators in Delhi",
                },
                "country": {
                    "type": "string",
                    "description": "Country filter, for example India",
                },
                "city": {
                    "type": "string",
                    "description": "City filter, for example Delhi",
                },
                "niches": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Creator niches, for example food blogging, fashion, travel",
                },
                "min_followers": {
                    "type": "integer",
                    "description": "Minimum follower count",
                },
                "max_followers": {
                    "type": "integer",
                    "description": "Maximum follower count",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of creators to return",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
]


def run_agent(user_message: str) -> list[dict[str, Any]]:
    fallback_arguments = _build_fallback_search_arguments(user_message)
    openai_client = _build_openai_client()

    if openai_client is None:
        return search_creator_profiles(**fallback_arguments)

    try:
        response = openai_client.responses.create(
            model=DEFAULT_OPENAI_MODEL,
            input=user_message,
            tools=tools,
            tool_choice={
                "type": "function",
                "name": "search_creator_profiles",
            },
        )

        for item in response.output:
            if item.type != "function_call":
                continue

            if item.name != "search_creator_profiles":
                continue

            arguments = json.loads(item.arguments)
            search_arguments = _normalize_tool_arguments(arguments, fallback_arguments)
            return search_creator_profiles(**search_arguments)

    except Exception as exc:
        logger.warning(
            "Creator search OpenAI planning failed. Falling back to local parsing: %s",
            exc,
        )

    return search_creator_profiles(**fallback_arguments)


if __name__ == "__main__":
    user_query = """
    Find 10 beauty creators in India.
    Show username, followers, following, niche, location, profile URL and short reason.
    """

    answer = run_agent(user_query)

    print(json.dumps(answer, ensure_ascii=False, indent=2, default=str))
