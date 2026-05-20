from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import Browser
from playwright.async_api import BrowserContext
from playwright.async_api import Playwright
from playwright.async_api import Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from datetime import datetime, timezone
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


load_dotenv()
logger = logging.getLogger(__name__)

client = None
if OpenAI is not None and os.getenv("OPENAI_API_KEY"):
    try:
        client = OpenAI(max_retries=0)
    except Exception as exc:
        logger.warning("OpenAI client setup failed: %s", exc)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


MONGO_URI = "mongodb://localhost:27017"
MONGO_DB_NAME = "instagpy"
MONGO_COLLECTION_NAME = "google_search_influencers"

SEARCH_MAX_CONCURRENT_REQUESTS = _env_int(
    "INSTAGRAM_SEARCH_MAX_CONCURRENT_REQUESTS",
    default=1,
    minimum=1,
)

SEARCH_PARALLEL_QUERY_TABS = _env_int(
    "INSTAGRAM_SEARCH_PARALLEL_QUERY_TABS",
    default=_env_int(
        "INSTAGRAM_SEARCH_MAX_URLS_PER_REQUEST",
        default=2,
        minimum=1,
    ),
    minimum=1,
)

SEARCH_GOTO_TIMEOUT_MS = _env_int(
    "INSTAGRAM_SEARCH_GOTO_TIMEOUT_MS",
    default=15000,
    minimum=500,
)

SEARCH_RESULT_WAIT_TIMEOUT_MS = _env_int(
    "INSTAGRAM_SEARCH_RESULT_WAIT_TIMEOUT_MS",
    default=3000,
    minimum=200,
)

SEARCH_PAGINATION_PAGES = _env_int(
    "INSTAGRAM_SEARCH_PAGINATION_PAGES",
    default=5,
    minimum=1,
)

SEARCH_ESTIMATED_RESULTS_PER_PAGE = _env_int(
    "INSTAGRAM_SEARCH_ESTIMATED_RESULTS_PER_PAGE",
    default=8,
    minimum=1,
)

SEARCH_MAX_QUERIES_PER_REQUEST = _env_int(
    "INSTAGRAM_SEARCH_MAX_QUERIES_PER_REQUEST",
    default=10,
    minimum=1,
)

SEARCH_OVERSAMPLE_FACTOR = _env_float(
    "INSTAGRAM_SEARCH_OVERSAMPLE_FACTOR",
    default=2.5,
    minimum=1.0,
)

SEARCH_QUERY_GEN_TIMEOUT_SECONDS = _env_float(
    "INSTAGRAM_SEARCH_QUERY_GEN_TIMEOUT_SECONDS",
    default=30.0,
    minimum=2,
)

MIN_FOLLOWERS_COUNT = _env_int(
    "INSTAGRAM_SEARCH_MIN_FOLLOWERS_COUNT",
    default=50000,
    minimum=0,
)

REQUIRE_SEARCH_FOLLOWERS_COUNT = os.getenv(
    "INSTAGRAM_SEARCH_REQUIRE_SEARCH_FOLLOWERS",
    "0",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

OPENAI_QUERY_MODEL = "gpt-5.4-mini"

# Always use Playwright's own Chromium browser.
# Do NOT connect to personal Chrome through CDP.

_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
_DUCKDUCKGO_RESULT_SELECTOR = "a.result__a, a[href*='instagram.com']"

_request_semaphore = asyncio.Semaphore(SEARCH_MAX_CONCURRENT_REQUESTS)
_browser_lock = asyncio.Lock()

_playwright: Playwright | None = None
_shared_browser: Browser | None = None


BLOCKED_FIRST_PATHS = {
    "p",
    "reel",
    "reels",
    "stories",
    "explore",
    "accounts",
    "tv",
    "about",
    "developer",
    "directory",
    "web",
    "api",
    "popular",
}

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
    "united states of america": "United States",
    "united states": "United States",
    "u.s.a": "United States",
    "u.s.": "United States",
    "usa": "United States",
    "us": "United States",
    "america": "United States",
    "american": "United States",
    "united kingdom": "United Kingdom",
    "u.k.": "United Kingdom",
    "uk": "United Kingdom",
    "britain": "United Kingdom",
    "british": "United Kingdom",
    "england": "United Kingdom",
    "uae": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    "india": "India",
    "indian": "India",
    "canada": "Canada",
    "canadian": "Canada",
    "australia": "Australia",
    "australian": "Australia",
    "singapore": "Singapore",
    "singaporean": "Singapore",
}

LOCATION_STOPWORDS = {
    "accounts",
    "around",
    "audience",
    "blogger",
    "bloggers",
    "collaborations",
    "content",
    "contact",
    "creator",
    "creators",
    "email",
    "followers",
    "for",
    "from",
    "having",
    "in",
    "influencer",
    "influencers",
    "instagram",
    "located",
    "near",
    "of",
    "official",
    "on",
    "posts",
    "profiles",
    "public",
    "reels",
    "reviewer",
    "reviews",
    "site",
    "site:instagram.com",
    "users",
    "with",
}


def _extract_requested_profile_count_value(user_prompt: str) -> int | None:
    prompt = re.sub(r"\s+", " ", (user_prompt or "").strip()).lower()

    if not prompt:
        return None

    digit_patterns = (
        r"\b(?P<count>\d{1,3})(?!\s*[km]\b)\s+(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b",
        r"\b(?:need|want|find|get|show|give)\s+(?P<count>\d{1,3})(?!\s*[km]\b)\b",
        r"\b(?P<count>\d{1,3})(?!\s*[km]\b)\b",
    )

    for pattern in digit_patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group("count")))

    word_pattern = (
        r"\b(?:need|want|find|get|show|give)\s+"
        r"(?P<count>one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
        r"eighteen|nineteen|twenty)\b"
    )
    match = re.search(word_pattern, prompt, flags=re.IGNORECASE)
    if match:
        return COUNT_WORDS.get(match.group("count").lower())

    leading_word_pattern = (
        r"\b(?P<count>one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
        r"eighteen|nineteen|twenty)\s+"
        r"(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?)\b"
    )
    match = re.search(leading_word_pattern, prompt, flags=re.IGNORECASE)
    if match:
        return COUNT_WORDS.get(match.group("count").lower())

    return None


def _extract_requested_profile_count(user_prompt: str, default: int = 10) -> int:
    parsed_count = _extract_requested_profile_count_value(user_prompt)
    if parsed_count is None:
        return default
    return parsed_count


def _strip_requested_profile_count(user_prompt: str) -> str:
    prompt = re.sub(r"\s+", " ", (user_prompt or "").strip())

    if not prompt:
        return ""

    replacements = (
        r"\b(?:need|want|find|get|show|give)\s+\d{1,4}(?!\s*[km]\b)\s+",
        r"\b\d{1,4}(?!\s*[km]\b)\s+(?:instagram\s+)?(?:usernames?|profiles?|accounts?|creators?|influencers?|leads?)\b",
        r"^\d{1,4}(?!\s*[km]\b)\s+",
    )

    cleaned = prompt
    for pattern in replacements:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    return re.sub(r"\s+", " ", cleaned).strip(" ,")


def _normalize_country_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = re.sub(r"\s+", " ", value).strip(" ,.")
    if not normalized:
        return None

    return COUNTRY_ALIASES.get(normalized.lower(), normalized.title())


def _normalize_city_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = re.sub(r"\s+", " ", value).strip(" ,.")
    if not normalized:
        return None

    if normalized.lower() in COUNTRY_ALIASES:
        return None

    return normalized.title()


def _trim_location_fragment(value: str | None) -> str:
    if not isinstance(value, str):
        return ""

    fragment = re.sub(r"\s+", " ", value).strip(" ,.")
    if not fragment:
        return ""

    fragment = re.split(
        (
            r"\b(?:with|having|above|below|over|under|around|near|followers?|"
            r"following|posts?|creator|creators|influencer|influencers|"
            r"blogger|bloggers|profiles?|accounts?|users?|instagram|site:instagram\.com)\b"
        ),
        fragment,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    return fragment.strip(" ,.")


def _extract_location_from_text(text: str) -> tuple[str | None, str | None]:
    if not isinstance(text, str):
        return None, None

    normalized_text = re.sub(r"\s+", " ", text).strip()
    if not normalized_text:
        return None, None

    city: str | None = None
    country: str | None = None

    for alias in sorted(COUNTRY_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", normalized_text, flags=re.IGNORECASE):
            country = COUNTRY_ALIASES[alias]
            break

    location_match = re.search(
        r"\b(?:in|from|based in|located in|out of|near)\s+([a-zA-Z][a-zA-Z\s,.'-]{1,60})",
        normalized_text,
        flags=re.IGNORECASE,
    )
    if location_match:
        location_fragment = _trim_location_fragment(location_match.group(1))

        if location_fragment:
            parts = [part.strip() for part in location_fragment.split(",") if part.strip()]
            fragment_country = _normalize_country_name(location_fragment)

            if parts:
                city = _normalize_city_name(parts[0])

            if len(parts) > 1:
                country = _normalize_country_name(parts[-1]) or country
            elif country and fragment_country == country:
                city = None
            elif country:
                city_candidate = location_fragment
                for alias, canonical_name in COUNTRY_ALIASES.items():
                    if canonical_name != country:
                        continue

                    city_candidate = re.sub(
                        rf"\b{re.escape(alias)}\b",
                        "",
                        city_candidate,
                        flags=re.IGNORECASE,
                    )

                city_candidate = city_candidate.strip(" ,-")
                city = _normalize_city_name(city_candidate) or city

    if city and city.lower() in LOCATION_STOPWORDS:
        city = None

    return city, country


def _extract_location_metadata(
    data: dict[str, object] | None,
    user_prompt: str,
) -> tuple[str | None, str | None]:
    prompt_city, prompt_country = _extract_location_from_text(user_prompt)

    if not isinstance(data, dict):
        return prompt_city, prompt_country

    city = _normalize_city_name(data.get("city"))
    country = _normalize_country_name(data.get("country"))

    if not city or not country:
        location_city, location_country = _extract_location_from_text(
            str(data.get("location", ""))
        )
        city = city or location_city
        country = country or location_country

    return city or prompt_city, country or prompt_country


def _calculate_search_query_count(profiles_limit: int) -> int:
    if profiles_limit <= 0:
        return 1

    estimated_profiles_per_query = max(
        1,
        SEARCH_PAGINATION_PAGES * SEARCH_ESTIMATED_RESULTS_PER_PAGE,
    )
    requested_query_count = int(
        (profiles_limit * SEARCH_OVERSAMPLE_FACTOR + estimated_profiles_per_query - 1)
        // estimated_profiles_per_query
    )

    return max(1, min(SEARCH_MAX_QUERIES_PER_REQUEST, requested_query_count))


def _build_fallback_queries(user_prompt: str, n: int) -> list[str]:
    cleaned_prompt = _strip_requested_profile_count(user_prompt)

    if not cleaned_prompt:
        return []

    candidates = [
        f"site:instagram.com {cleaned_prompt}",
        f'site:instagram.com "{cleaned_prompt}"',
        f"site:instagram.com {cleaned_prompt} followers following posts",
        f"site:instagram.com {cleaned_prompt} influencer Instagram",
        f"site:instagram.com {cleaned_prompt} creator Instagram",
        f"site:instagram.com {cleaned_prompt} blogger",
        f"site:instagram.com {cleaned_prompt} public figure",
        f"site:instagram.com {cleaned_prompt} official",
        f"site:instagram.com {cleaned_prompt} contact email",
        f"site:instagram.com {cleaned_prompt} collaborations",
        f"site:instagram.com {cleaned_prompt} reels",
        f"site:instagram.com {cleaned_prompt} posts",
        f'site:instagram.com "{cleaned_prompt}" "followers"',
        f'site:instagram.com "{cleaned_prompt}" "posts"',
        f"Instagram influencers {cleaned_prompt} site:instagram.com",
        f"Instagram creators {cleaned_prompt} site:instagram.com",
        f"Instagram bloggers {cleaned_prompt} site:instagram.com",
    ]

    queries: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", candidate).strip()
        if not normalized:
            continue

        key = normalized.lower()
        if key in seen:
            continue

        seen.add(key)
        queries.append(normalized)

        if len(queries) >= max(1, n):
            break

    return queries


def _build_fallback_payload(user_prompt: str, n: int) -> dict[str, object]:
    city, country = _extract_location_from_text(user_prompt)

    return {
        "queries": _build_fallback_queries(user_prompt, n),
        "profiles_required": _extract_requested_profile_count(user_prompt),
        "city": city,
        "country": country,
    }


def _fallback_result_from_prompt(user_prompt: str, n: int) -> dict[str, object]:
    payload = _build_fallback_payload(user_prompt, n)

    return {
        "queries": list(payload["queries"])[: max(1, n)],
        "limit": int(payload["profiles_required"]),
        "city": payload.get("city"),
        "country": payload.get("country"),
    }


def generate_google_urls_from_prompt(user_prompt: str, n: int = 5) -> dict[str, object]:
    fallback_payload = _build_fallback_payload(user_prompt, n)
    search_prompt = _strip_requested_profile_count(user_prompt) or user_prompt

    if client is None:
        return _fallback_result_from_prompt(user_prompt, n)

    prompt = f"""
    You are helping generate DuckDuckGo search queries to find Instagram profile URLs.

    User request:
    {user_prompt}

    Cleaned search topic:
    {search_prompt}

    Task:
    Generate {n} DuckDuckGo search queries to find Instagram creator/influencer profile URLs.

    Important:
    - Search engines often do not index exact Instagram follower counts.
    - Do NOT make every query too strict with exact follower count like "100K followers".
    - Use broad discovery queries first.
    - Mention follower-related words like followers/posts only in some queries.
    - If user mentions follower count like 100K, 300K, 1M, treat it as minimum_followers, not as profiles_required.
    - profiles_required means how many profiles/usernames the user wants, not follower count.
    - Prefer Instagram profile/account pages.
    - It is okay if search returns posts/reels because code will filter URLs later, but queries should prefer profile discovery.
    - Use site:instagram.com.
    - Keep same niche/category.
    - Keep same city/location.
    - Keep same audience context.
    - Use niche synonyms.

    Return ONLY valid JSON.

    JSON format:
    {{
    "queries": [
        "site:instagram.com Mumbai food creator followers",
        "site:instagram.com Mumbai food influencer",
        "site:instagram.com Mumbai food blogger",
        "site:instagram.com Mumbai foodie",
        "site:instagram.com restaurant reviewer Mumbai"
    ],
    "profiles_required": 10,
    "minimum_followers": 100000,
    "niche": "food",
    "location": "Mumbai, India",
    "city": "Mumbai",
    "country": "India"
    }}
    """

    try:
        response = client.responses.create(
            model=OPENAI_QUERY_MODEL,
            input=prompt,
        )
        data = json.loads(response.output_text)

        if not isinstance(data, dict):
            data = fallback_payload

    except Exception as exc:
        logger.warning(
            "OpenAI query generation failed. Using fallback Google queries: %s",
            exc,
        )
        data = fallback_payload

    raw_queries = data.get("queries", [])

    queries: list[str] = []
    seen_queries: set[str] = set()

    if isinstance(raw_queries, list):
        for raw_query in raw_queries:
            if not isinstance(raw_query, str):
                continue

            normalized_query = re.sub(r"\s+", " ", raw_query).strip()
            if not normalized_query:
                continue

            key = normalized_query.lower()
            if key in seen_queries:
                continue

            seen_queries.add(key)
            queries.append(normalized_query)

    if not queries:
        queries = list(fallback_payload["queries"])
    elif len(queries) < max(1, n):
        for fallback_query in fallback_payload["queries"]:
            if not isinstance(fallback_query, str):
                continue

            normalized_query = re.sub(r"\s+", " ", fallback_query).strip()
            if not normalized_query:
                continue

            key = normalized_query.lower()
            if key in seen_queries:
                continue

            seen_queries.add(key)
            queries.append(normalized_query)

            if len(queries) >= max(1, n):
                break

    try:
        profiles_limit = int(data.get("profiles_required", 0))
    except (TypeError, ValueError):
        profiles_limit = 0

    if profiles_limit <= 0:
        profiles_limit = int(fallback_payload["profiles_required"])

    city, country = _extract_location_metadata(data, user_prompt)

    return {
        "queries": queries[: max(1, n)],
        "limit": profiles_limit,
        "city": city,
        "country": country,
    }


async def _generate_google_urls_from_prompt_async(
    user_prompt: str,
    n: int = 4,
) -> dict[str, object]:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(generate_google_urls_from_prompt, user_prompt, n),
            timeout=SEARCH_QUERY_GEN_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError:
        logger.warning(
            "Google query generation timed out after %.1fs. Using fallback.",
            SEARCH_QUERY_GEN_TIMEOUT_SECONDS,
        )
        return _fallback_result_from_prompt(user_prompt, n)

    except Exception as exc:
        logger.warning("Google query generation failed. Using fallback: %s", exc)
        return _fallback_result_from_prompt(user_prompt, n)

    if not isinstance(result, dict):
        return _fallback_result_from_prompt(user_prompt, n)

    queries = result.get("queries")

    try:
        limit = int(result.get("limit", 0))
    except (TypeError, ValueError):
        limit = 0

    if not isinstance(queries, list) or limit <= 0:
        return _fallback_result_from_prompt(user_prompt, n)

    queries = [query for query in queries if isinstance(query, str) and query.strip()]

    if not queries:
        return _fallback_result_from_prompt(user_prompt, n)

    city, country = _extract_location_metadata(result, user_prompt)

    return {
        "queries": queries,
        "limit": limit,
        "city": city,
        "country": country,
    }


def _parse_count_to_int(value: str | None) -> int | None:
    if not value:
        return None

    text = value.strip().lower()
    text = text.replace(",", "")
    text = text.replace("+", "")
    text = text.replace(" ", "")

    multiplier = 1

    if text.endswith("crore"):
        multiplier = 10_000_000
        text = text.replace("crore", "")
    elif text.endswith("cr"):
        multiplier = 10_000_000
        text = text.replace("cr", "")
    elif text.endswith("lakh"):
        multiplier = 100_000
        text = text.replace("lakh", "")
    elif text.endswith("lac"):
        multiplier = 100_000
        text = text.replace("lac", "")
    elif text.endswith("l"):
        multiplier = 100_000
        text = text[:-1]
    elif text.endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.endswith("b"):
        multiplier = 1_000_000_000
        text = text[:-1]

    try:
        return int(float(text) * multiplier)
    except Exception:
        return None


def _extract_metric(text: str, label: str) -> tuple[str | None, int | None]:
    if not text:
        return None, None

    patterns = [
        rf"(?P<count>\d+(?:[.,]\d+)?\s*(?:K|M|B|L|Cr|Crore|Lakh|Lac)?\+?)\s*{label}",
        rf"(?P<count>\d+(?:[.,]\d+)?)\s*{label}",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            raw = re.sub(r"\s+", "", match.group("count"))
            return raw, _parse_count_to_int(raw)

    return None, None


def _decode_google_url(raw_url: str) -> str:
    raw_url = raw_url.strip().replace("&amp;", "&")
    raw_url = unquote(raw_url)

    parsed = urlparse(raw_url)

    if parsed.netloc.lower().endswith("google.com") and parsed.path == "/url":
        qs = parse_qs(parsed.query)
        for key in ("q", "url"):
            values = qs.get(key)
            if values:
                return values[0]

    if raw_url.startswith("/url?"):
        qs = parse_qs(urlparse(raw_url).query)
        for key in ("q", "url"):
            values = qs.get(key)
            if values:
                return values[0]

    if parsed.netloc.lower() in {"duckduckgo.com", "www.duckduckgo.com"}:
        qs = parse_qs(parsed.query)
        for key in ("uddg", "rut"):
            values = qs.get(key)
            if values:
                return values[0]

    return raw_url


def is_real_instagram_profile_url(url: str) -> bool:
    try:
        decoded_url = _decode_google_url(url)
        parsed = urlsplit(decoded_url)
    except Exception:
        return False

    if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        return False

    path = parsed.path.strip("/")
    if not path:
        return False

    parts = path.split("/")
    if len(parts) != 1:
        return False

    username = parts[0].strip()

    if username.lower() in BLOCKED_FIRST_PATHS:
        return False

    if not re.match(r"^[A-Za-z0-9._]{1,30}$", username):
        return False

    return True


def normalize_profile_url(url: str) -> str | None:
    try:
        decoded_url = _decode_google_url(url)
        parsed = urlsplit(decoded_url)
    except Exception:
        return None

    if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        return None

    path = parsed.path.strip("/")
    if not path:
        return None

    parts = path.split("/")
    if len(parts) != 1:
        return None

    username = parts[0].strip()

    if username.lower() in BLOCKED_FIRST_PATHS:
        return None

    if not re.match(r"^[A-Za-z0-9._]{1,30}$", username):
        return None

    return f"https://www.instagram.com/{username}/"


def _get_best_google_result_text(anchor) -> str:
    best_text = anchor.get_text(" ", strip=True)

    for parent in anchor.parents:
        if parent.name not in {"div", "article", "section"}:
            continue

        text = parent.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            continue

        if len(text) > 1800:
            continue

        lower_text = text.lower()

        if (
            "instagram" in lower_text
            or "followers" in lower_text
            or "following" in lower_text
            or "posts" in lower_text
        ):
            if len(text) > len(best_text):
                best_text = text

    return best_text


def _extract_title(anchor, result_text: str) -> str | None:
    h3 = anchor.select_one("h3")
    if h3:
        title = h3.get_text(" ", strip=True)
        if title:
            return title

    anchor_text = anchor.get_text(" ", strip=True)
    if anchor_text:
        return anchor_text

    if result_text:
        return result_text[:120]

    return None


def extract_google_instagram_profiles_with_counts_from_html(
    html: str,
    source_url: str | None = None,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()

        if not href:
            continue

        profile_url = normalize_profile_url(href)

        if not profile_url:
            continue

        if profile_url in seen:
            continue

        seen.add(profile_url)

        username = profile_url.rstrip("/").split("/")[-1]
        result_text = _get_best_google_result_text(anchor)
        title = _extract_title(anchor, result_text)

        followers_raw, followers_count = _extract_metric(result_text, "followers?")
        following_raw, following_count = _extract_metric(result_text, "following")
        posts_raw, posts_count = _extract_metric(result_text, "posts?")

        row = {
            "username": username,
            "profile_url": profile_url,
            "title": title,
            "followers": followers_raw,
            "followers_count": followers_count,
            "following": following_raw,
            "following_count": following_count,
            "posts": posts_raw,
            "posts_count": posts_count,
            "snippet": result_text,
            "source": "duckduckgo_search",
            "source_url": source_url,
        }

        results.append(row)

    return results


async def _block_heavy_resources(route: Route) -> None:
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return

    await route.continue_()


async def _ensure_browser() -> Browser:
    global _playwright
    global _shared_browser

    if _shared_browser is not None and _shared_browser.is_connected():
        return _shared_browser

    async with _browser_lock:
        if _shared_browser is not None and _shared_browser.is_connected():
            return _shared_browser

        if _playwright is None:
            _playwright = await async_playwright().start()

        browser_args = ["--disable-dev-shm-usage"]

        if os.getenv("PLAYWRIGHT_NO_SANDBOX", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            browser_args.append("--no-sandbox")

        # This launches Playwright-managed Chromium.
        # It does not use your personal Chrome profile, bookmarks, cookies, or login session.
        _shared_browser = await _playwright.chromium.launch(
            headless=True,
            channel="chromium",
            args=browser_args,
        )

        return _shared_browser


async def close_shared_browser() -> None:
    global _playwright
    global _shared_browser

    async with _browser_lock:
        if _shared_browser is not None:
            try:
                await _shared_browser.close()
            except Exception as exc:
                logger.debug("Failed to close browser: %s", exc)
            finally:
                _shared_browser = None

        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception as exc:
                logger.debug("Failed to stop Playwright: %s", exc)
            finally:
                _playwright = None


async def _submit_duckduckgo_search(page, query: str) -> None:
    await page.goto(
        _DUCKDUCKGO_HTML_URL,
        timeout=SEARCH_GOTO_TIMEOUT_MS,
        wait_until="domcontentloaded",
    )

    try:
        search_input = page.locator(
            "#search_form_input_homepage, input[name='q']"
        ).first
        await search_input.wait_for(timeout=SEARCH_RESULT_WAIT_TIMEOUT_MS)
        await search_input.click()
        await search_input.fill(query)

        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=SEARCH_GOTO_TIMEOUT_MS,
            ):
                await search_input.press("Enter")
        except PlaywrightTimeoutError:
            submit_button = page.locator(
                "input[type='submit'], button[type='submit']"
            ).first
            async with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=SEARCH_GOTO_TIMEOUT_MS,
            ):
                await submit_button.click()

        await page.locator(_DUCKDUCKGO_RESULT_SELECTOR).first.wait_for(
            timeout=max(8000, SEARCH_RESULT_WAIT_TIMEOUT_MS),
        )
    except Exception:
        html = await page.content()
        with open("debug_duckduckgo_search.html", "w", encoding="utf-8") as f:
            f.write(html)

        try:
            await page.screenshot(
                path="debug_duckduckgo_search.png",
                full_page=True,
            )
        except Exception:
            pass

        raise


async def _goto_next_duckduckgo_results_page(page) -> bool:
    next_candidates = [
        page.get_by_role("button", name=re.compile(r"next", re.I)).first,
        page.get_by_role("link", name=re.compile(r"next", re.I)).first,
        page.locator("input[value='Next'], button[value='Next']").first,
        page.locator("a.result--more__btn, .nav-link").first,
    ]

    for next_button in next_candidates:
        try:
            await next_button.wait_for(timeout=SEARCH_RESULT_WAIT_TIMEOUT_MS)

            try:
                async with page.expect_navigation(
                    wait_until="domcontentloaded",
                    timeout=SEARCH_GOTO_TIMEOUT_MS,
                ):
                    await next_button.click()
            except PlaywrightTimeoutError:
                await next_button.click()
                await page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=SEARCH_GOTO_TIMEOUT_MS,
                )

            await page.locator(_DUCKDUCKGO_RESULT_SELECTOR).first.wait_for(
                timeout=SEARCH_RESULT_WAIT_TIMEOUT_MS
            )
            return True
        except Exception:
            continue

    return False


async def _scrape_profiles_from_single_query(
    context: BrowserContext,
    query: str,
) -> list[dict[str, Any]]:
    page = await context.new_page()

    try:
        await page.route("**/*", _block_heavy_resources)
        await _submit_duckduckgo_search(page, query)

        try:
            await page.locator("body").first.wait_for(
                timeout=SEARCH_RESULT_WAIT_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return []

        await page.wait_for_timeout(1000)

        try:
            await page.locator("a[href]").first.wait_for(
                timeout=SEARCH_RESULT_WAIT_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return []

        results: list[dict[str, Any]] = []
        seen_profile_urls: set[str] = set()

        for page_index in range(SEARCH_PAGINATION_PAGES):
            full_html = await page.content()
            print("Final URL:", page.url)
            print("Page title:", await page.title())
            page_results = extract_google_instagram_profiles_with_counts_from_html(
                full_html,
                source_url=page.url,
            )

            for item in page_results:
                profile_url = item.get("profile_url")

                if not isinstance(profile_url, str) or not profile_url.strip():
                    continue

                if profile_url in seen_profile_urls:
                    continue

                seen_profile_urls.add(profile_url)
                item["source_query"] = query
                results.append(item)

            if page_index >= SEARCH_PAGINATION_PAGES - 1:
                break

            moved = await _goto_next_duckduckgo_results_page(page)
            if not moved:
                break

            await page.wait_for_timeout(1000)

        return results

    except PlaywrightTimeoutError:
        return []

    except Exception as exc:
        logger.warning("Failed scraping DuckDuckGo search query %s: %s", query, exc)
        return []

    finally:
        try:
            await page.close()
        except Exception:
            pass


def _passes_min_followers_filter(item: dict[str, Any]) -> bool:
    if MIN_FOLLOWERS_COUNT <= 0:
        return True

    followers_count = item.get("followers_count")

    if followers_count is None:
        return not REQUIRE_SEARCH_FOLLOWERS_COUNT

    try:
        return int(followers_count) >= MIN_FOLLOWERS_COUNT
    except Exception:
        return not REQUIRE_SEARCH_FOLLOWERS_COUNT


async def run_user_search(query: str) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    seen_profile_urls: set[str] = set()
    explicit_requested_profiles = _extract_requested_profile_count_value(query)
    requested_profiles = explicit_requested_profiles or _extract_requested_profile_count(query)
    profiles_limit = max(1, requested_profiles)
    search_query_count = _calculate_search_query_count(profiles_limit)

    query_generation_count = max(SEARCH_PARALLEL_QUERY_TABS, search_query_count)

    llm_res = await _generate_google_urls_from_prompt_async(
        user_prompt=query,
        n=query_generation_count,
    )

    llm_profiles_limit = int(llm_res.get("limit", 0))
    queries = llm_res.get("queries", [])

    if llm_profiles_limit > 0 and explicit_requested_profiles is None:
        profiles_limit = max(profiles_limit, llm_profiles_limit)

    if not isinstance(queries, list) or not queries:
        return data[:profiles_limit]

    search_queries = [
        str(search_query).strip()
        for search_query in queries
        if isinstance(search_query, str) and search_query.strip()
    ]

    if not search_queries:
        return data[:profiles_limit]

    logger.info(
        "DuckDuckGo search plan: requested=%s limit=%s queries=%s parallel_tabs=%s pages_per_query=%s",
        requested_profiles,
        profiles_limit,
        len(search_queries),
        SEARCH_PARALLEL_QUERY_TABS,
        SEARCH_PAGINATION_PAGES,
    )

    async with _request_semaphore:
        browser = await _ensure_browser()

        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        close_context = True

        try:
            max_query_tabs = max(
                1,
                min(SEARCH_PARALLEL_QUERY_TABS, len(search_queries)),
            )

            query_tab_semaphore = asyncio.Semaphore(max_query_tabs)

            async def scrape_with_limit(search_query: str) -> list[dict[str, Any]]:
                async with query_tab_semaphore:
                    return await _scrape_profiles_from_single_query(
                        context,
                        search_query,
                    )

            tasks = [
                asyncio.create_task(scrape_with_limit(search_query))
                for search_query in search_queries
            ]

            scraped_batches = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        finally:
            if close_context:
                await context.close()

    for batch in scraped_batches:
        if isinstance(batch, Exception):
            logger.warning("Google scraping worker failed: %s", batch)
            continue

        if not isinstance(batch, list):
            continue

        for item in batch:
            if not isinstance(item, dict):
                continue

            profile_url = item.get("profile_url")

            if not isinstance(profile_url, str) or not profile_url.strip():
                continue

            profile_url = normalize_profile_url(profile_url)

            if not profile_url:
                continue

            if profile_url in seen_profile_urls:
                continue

            if not _passes_min_followers_filter(item):
                continue

            seen_profile_urls.add(profile_url)

            username = item.get("username") or profile_url.rstrip("/").split("/")[-1]

            final_item = dict(item)
            final_item["username"] = username
            final_item["profile_url"] = profile_url

            data.append(final_item)

            if len(data) >= profiles_limit:
                break

        if len(data) >= profiles_limit:
            break

    for item in data:
        try:
            if not isinstance(item, dict):
                continue

            item["city"] = llm_res.get("city") if isinstance(llm_res, dict) else None
            item["country"] = llm_res.get("country") if isinstance(llm_res, dict) else None

        except Exception as e:
            print(f"Error while updating item: {e}")
            continue

    save_profiles_to_mongodb(
        profiles=data[:profiles_limit],
        search_query=query,
    )

    return data[:profiles_limit]


def _get_mongo_collection():
    mongo_client = MongoClient(MONGO_URI)

    db = mongo_client[MONGO_DB_NAME]
    collection = db[MONGO_COLLECTION_NAME]

    # This prevents duplicate profile_url entries.
    collection.create_index("profile_url", unique=True)

    # Helpful indexes for future filtering/searching.
    collection.create_index("username")
    collection.create_index("followers_count")
    collection.create_index("search_query")
    collection.create_index("created_at")

    return mongo_client, collection


def save_profiles_to_mongodb(
    profiles: list[dict[str, Any]],
    search_query: str,
) -> dict[str, int]:
    if not profiles:
        return {
            "found": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
        }

    mongo_client, collection = _get_mongo_collection()

    now = datetime.now(timezone.utc)

    operations = []
    skipped = 0

    for profile in profiles:
        if not isinstance(profile, dict):
            skipped += 1
            continue

        raw_profile_url = profile.get("profile_url")

        if not isinstance(raw_profile_url, str) or not raw_profile_url.strip():
            skipped += 1
            continue

        profile_url = normalize_profile_url(raw_profile_url)

        if not profile_url:
            skipped += 1
            continue

        username = profile.get("username") or profile_url.rstrip("/").split("/")[-1]

        document = dict(profile)
        document["username"] = username
        document["profile_url"] = profile_url
        document["search_query"] = search_query
        document["database_source"] = "google_search_influencers"
        document["updated_at"] = now

        operations.append(
            UpdateOne(
                {"profile_url": profile_url},
                {
                    "$set": document,
                    "$setOnInsert": {
                        "created_at": now,
                    },
                },
                upsert=True,
            )
        )

    if not operations:
        mongo_client.close()
        return {
            "found": len(profiles),
            "inserted": 0,
            "updated": 0,
            "skipped": skipped,
        }

    try:
        result = collection.bulk_write(operations, ordered=False)

        inserted = len(result.upserted_ids)
        updated = result.modified_count

        mongo_client.close()

        return {
            "found": len(profiles),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
        }

    except BulkWriteError as exc:
        logger.warning("MongoDB bulk write error: %s", exc.details)

        mongo_client.close()

        return {
            "found": len(profiles),
            "inserted": 0,
            "updated": 0,
            "skipped": skipped,
        }

    except Exception as exc:
        mongo_client.close()
        raise RuntimeError(f"MongoDB save failed: {exc}") from exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    user_prompt = "10 comedy influencers or creators in india"

    data = asyncio.run(run_user_search(user_prompt))

    save_result = save_profiles_to_mongodb(
        profiles=data,
        search_query=user_prompt,
    )

    print("Saved to MongoDB")
    print(f"Database: {MONGO_DB_NAME}")
    print(f"Collection: {MONGO_COLLECTION_NAME}")
    print(f"Minimum followers filter: {MIN_FOLLOWERS_COUNT}")
    print(json.dumps(save_result, indent=2, ensure_ascii=False))
