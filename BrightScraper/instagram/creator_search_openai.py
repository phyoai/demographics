from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http.models import FieldCondition, Filter, MatchAny, MatchValue, Range
from qdrant_client.models import Document

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION_NAME = "instagram_creator_profiles"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_LIMIT = 10

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
    "india": "india",
    "indian": "india",
    "in": "india",
    "bharat": "india",
    "united states": "united_states",
    "usa": "united_states",
    "us": "united_states",
    "america": "united_states",
}

CITY_ALIASES = {
    "delhi": "delhi",
    "new delhi": "delhi",
    "nct delhi": "delhi",
    "delhi ncr": "delhi",
    "ncr": "delhi",
    "mumbai": "mumbai",
    "bombay": "mumbai",
    "bengaluru": "bengaluru",
    "bangalore": "bengaluru",
    "gurugram": "gurugram",
    "gurgaon": "gurugram",
    "noida": "noida",
    "hyderabad": "hyderabad",
    "pune": "pune",
    "chennai": "chennai",
    "kolkata": "kolkata",
    "calcutta": "kolkata",
    "ahmedabad": "ahmedabad",
    "jaipur": "jaipur",
    "lucknow": "lucknow",
    "surat": "surat",
    "indore": "indore",
    "chandigarh": "chandigarh",
    "kochi": "kochi",
    "goa": "goa",
}

NICHE_ALIASES = {
    "food": "food",
    "foodie": "food",
    "foodies": "food",
    "food blogger": "food",
    "food blogging": "food",
    "food influencer": "food",
    "restaurant": "food",
    "restaurants": "food",
    "restaurant review": "food",
    "restaurant reviews": "food",
    "cafe": "food",
    "cafes": "food",
    "recipe": "food",
    "recipes": "food",
    "cooking": "food",
    "baking": "food",
    "chef": "food",
    "food beverage": "food",
    "food and beverage": "food",
    "tech": "tech",
    "technology": "tech",
    "tech creator": "tech",
    "tech influencer": "tech",
    "gadget": "tech",
    "gadgets": "tech",
    "ai": "tech",
    "artificial intelligence": "tech",
    "software": "tech",
    "coding": "tech",
    "developer": "tech",
    "programming": "tech",
    "startup": "tech",
    "saas": "tech",
    "fashion": "fashion",
    "style": "fashion",
    "styling": "fashion",
    "outfit": "fashion",
    "outfits": "fashion",
    "beauty": "beauty",
    "makeup": "beauty",
    "skincare": "beauty",
    "travel": "travel",
    "traveller": "travel",
    "traveler": "travel",
    "fitness": "fitness",
    "gym": "fitness",
    "workout": "fitness",
    "yoga": "fitness",
    "lifestyle": "lifestyle",
    "finance": "finance",
    "investing": "finance",
    "business": "business",
    "education": "education",
    "edtech": "education",
    "gaming": "gaming",
    "photography": "photography",
    "parenting": "parenting",
    "health": "health",
    "wellness": "health",
    "comedy": "comedy",
    "entertainment": "entertainment",
    "art": "art",
    "music": "music",
    "sports": "sports",
    "automotive": "automotive",
    "cars": "automotive",
    "home decor": "home_decor",
    "interior": "home_decor",
    "interiors": "home_decor",
}


@dataclass(frozen=True)
class ParsedCreatorQuery:
    semantic_query: str
    country: str | None
    city: str | None
    niches: list[str]
    min_followers: int | None
    max_followers: int | None
    limit: int | None


def normalize_token(value: Any) -> str | None:
    if value is None:
        return None

    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def canonical_country(value: Any) -> str | None:
    token = normalize_token(value)
    if not token:
        return None
    return COUNTRY_ALIASES.get(token, token.replace(" ", "_"))


def canonical_city(value: Any) -> str | None:
    token = normalize_token(value)
    if not token:
        return None
    return CITY_ALIASES.get(token, token.replace(" ", "_"))


def canonical_niche(value: Any) -> str | None:
    token = normalize_token(value)
    if not token:
        return None

    if token in NICHE_ALIASES:
        return NICHE_ALIASES[token]

    for alias, canonical in sorted(NICHE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", token):
            return canonical

    return token.replace(" ", "_")


def normalize_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []

    seen: set[str] = set()
    values: list[str] = []
    for candidate in candidates:
        text = " ".join(str(candidate).split()).strip()
        if not text:
            continue

        key = text.casefold()
        if key in seen:
            continue

        seen.add(key)
        values.append(text)

    return values


def canonical_niche_list(value: Any) -> list[str]:
    seen: set[str] = set()
    niches: list[str] = []

    for item in normalize_text_list(value):
        niche = canonical_niche(item)
        if not niche or niche in seen:
            continue

        seen.add(niche)
        niches.append(niche)

    return niches


def extract_cities_from_text(text: str) -> list[str]:
    token = normalize_token(text) or ""
    cities: list[str] = []
    seen: set[str] = set()

    for alias, canonical in sorted(CITY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if canonical in seen:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", token):
            seen.add(canonical)
            cities.append(canonical)

    return cities


def extract_niches_from_text(text: str) -> list[str]:
    token = normalize_token(text) or ""
    niches: list[str] = []
    seen: set[str] = set()

    for alias, canonical in sorted(NICHE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if canonical in seen:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", token):
            seen.add(canonical)
            niches.append(canonical)

    return niches


def build_qdrant_client() -> QdrantClient:
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    if not qdrant_url or not qdrant_api_key:
        raise ValueError("Set both QDRANT_URL and QDRANT_API_KEY before running creator search.")

    return QdrantClient(url=qdrant_url, api_key=qdrant_api_key, cloud_inference=True)


def parse_compact_count(raw_number: str, suffix: str | None) -> int:
    number = float(raw_number)
    multiplier = 1
    if suffix:
        normalized_suffix = suffix.casefold()
        if normalized_suffix == "k":
            multiplier = 1_000
        elif normalized_suffix == "m":
            multiplier = 1_000_000

    return int(number * multiplier)


def parse_follower_constraints(query_text: str) -> tuple[int | None, int | None]:
    min_followers = None
    max_followers = None
    follower_patterns = (
        re.compile(
            r"\b(?P<direction>under|below|less than|max(?:imum)?|up to)\s+"
            r"(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>[km])?\s*(?:followers?|follower)?\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?P<direction>over|above|more than|at least|min(?:imum)?|with)?\s*"
            r"(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>[km])\+?\s*(?:followers?|follower)?\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?P<direction>over|above|more than|at least|min(?:imum)?|with)?\s*"
            r"(?P<number>\d{4,})\+?\s*(?P<suffix>)\s*followers?\b",
            re.IGNORECASE,
        ),
    )

    consumed_spans: list[tuple[int, int]] = []
    for pattern in follower_patterns:
        for match in pattern.finditer(query_text):
            span = match.span()
            if any(max(span[0], used[0]) < min(span[1], used[1]) for used in consumed_spans):
                continue

            consumed_spans.append(span)
            count = parse_compact_count(match.group("number"), match.groupdict().get("suffix"))
            direction = (match.groupdict().get("direction") or "min").casefold()
            if direction in {"under", "below", "less than", "max", "maximum", "up to"}:
                max_followers = count if max_followers is None else min(max_followers, count)
            else:
                min_followers = count if min_followers is None else max(min_followers, count)

    return min_followers, max_followers


def extract_limit(query_text: str, default: int = DEFAULT_LIMIT) -> int:
    token = normalize_token(query_text) or ""
    if not token:
        return default

    digit_patterns = (
        r"\b(?:need|want|find|get|show|give)\s+(?P<count>\d{1,3})(?!\s*[km]\b)\b",
        r"\b(?P<count>\d{1,3})(?!\s*[km]\b)\s+(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b",
        r"^(?P<count>\d{1,3})(?!\s*[km]\b)\b",
    )

    for pattern in digit_patterns:
        match = re.search(pattern, token, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group("count")))

    word_pattern = (
        r"\b(?P<count>one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
        r"eighteen|nineteen|twenty)\s+"
        r"(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b"
    )
    match = re.search(word_pattern, token, flags=re.IGNORECASE)
    if match:
        return COUNT_WORDS.get(match.group("count"), default)

    return default


def clean_semantic_query(query_text: str) -> str:
    cleaned = query_text
    cleaned = re.sub(r"\bindluencers\b", "influencers", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:in|from|based in)\s+[a-z][a-z\s]*(?:,?\s+india)?\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:need|want|find|get|show|give)\s+\d{1,3}(?!\s*[km]\b)\s+",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b\d{1,3}(?!\s*[km]\b)\s+(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:with|over|above|more than|at least|min(?:imum)?|under|below|less than|max(?:imum)?|up to)?\s*"
        r"\d+(?:\.\d+)?\s*[km]?\+?\s*followers?\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:influencers?|creators?|profiles?|accounts?)\b", " creator ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or query_text.strip()


def parse_creator_query(query_text: str) -> ParsedCreatorQuery:
    min_followers, max_followers = parse_follower_constraints(query_text)
    cities = extract_cities_from_text(query_text)
    niches = extract_niches_from_text(query_text)
    country = "india" if re.search(r"\bindia(?:n)?\b", query_text, re.IGNORECASE) or cities else None

    return ParsedCreatorQuery(
        semantic_query=clean_semantic_query(query_text),
        country=country,
        city=cities[0] if cities else None,
        niches=niches,
        min_followers=min_followers,
        max_followers=max_followers,
        limit=extract_limit(query_text),
    )


def build_filter(
    country: str | None = None,
    city: str | None = None,
    niches: list[str] | None = None,
    min_followers: int | None = None,
    max_followers: int | None = None,
) -> Filter | None:
    conditions: list[FieldCondition] = []
    country_norm = canonical_country(country)
    city_norm = canonical_city(city)
    niche_norm = canonical_niche_list(niches or [])

    if country_norm:
        conditions.append(FieldCondition(key="country_norm", match=MatchValue(value=country_norm)))
    if city_norm:
        conditions.append(FieldCondition(key="city_norm", match=MatchValue(value=city_norm)))
    if niche_norm:
        conditions.append(FieldCondition(key="niche_norm", match=MatchAny(any=niche_norm)))
    if min_followers is not None or max_followers is not None:
        conditions.append(FieldCondition(key="followers", range=Range(gte=min_followers, lte=max_followers)))

    if not conditions:
        return None

    return Filter(must=conditions)


def _resolve_limit(explicit_limit: int | None, parsed_limit: int | None) -> int:
    if explicit_limit is not None:
        return max(1, explicit_limit)
    if parsed_limit is not None:
        return max(1, parsed_limit)
    return DEFAULT_LIMIT


def search_creator_profiles(
    query: str,
    collection_name: str | None = None,
    country: str | None = None,
    city: str | None = None,
    niches: list[str] | None = None,
    min_followers: int | None = None,
    max_followers: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    parsed_query = parse_creator_query(query)
    resolved_country = canonical_country(country) or parsed_query.country
    resolved_city = canonical_city(city) or parsed_query.city
    resolved_niches = canonical_niche_list(niches or []) or parsed_query.niches
    resolved_min_followers = min_followers if min_followers is not None else parsed_query.min_followers
    resolved_max_followers = max_followers if max_followers is not None else parsed_query.max_followers
    resolved_limit = _resolve_limit(limit, parsed_query.limit)
    resolved_collection = collection_name or os.getenv("QDRANT_COLLECTION", DEFAULT_COLLECTION_NAME)

    query_filter = build_filter(
        country=resolved_country,
        city=resolved_city,
        niches=resolved_niches,
        min_followers=resolved_min_followers,
        max_followers=resolved_max_followers,
    )

    logger.info(
        "Creator search query=%r semantic_query=%r collection=%s filters=%s",
        query,
        parsed_query.semantic_query,
        resolved_collection,
        {
            "country": resolved_country,
            "city": resolved_city,
            "niches": resolved_niches,
            "min_followers": resolved_min_followers,
            "max_followers": resolved_max_followers,
            "limit": resolved_limit,
        },
    )

    client = build_qdrant_client()
    results = client.query_points(
        collection_name=resolved_collection,
        query=Document(text=parsed_query.semantic_query, model=DEFAULT_EMBEDDING_MODEL),
        query_filter=query_filter,
        with_payload=True,
        limit=resolved_limit,
    )

    final_data: list[dict[str, Any]] = []
    for point in results.points:
        payload = dict(point.payload or {})
        payload.setdefault("score", point.score)
        final_data.append(payload)

    return final_data


def run_agent(
    user_message: str,
    *,
    collection_name: str | None = None,
    country: str | None = None,
    city: str | None = None,
    niches: list[str] | None = None,
    min_followers: int | None = None,
    max_followers: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return search_creator_profiles(
        query=user_message,
        collection_name=collection_name,
        country=country,
        city=city,
        niches=niches,
        min_followers=min_followers,
        max_followers=max_followers,
        limit=limit,
    )


if __name__ == "__main__":
    import json

    answer = run_agent("Find 10 beauty creators in India.")
    print(json.dumps(answer, ensure_ascii=False, indent=2, default=str))
