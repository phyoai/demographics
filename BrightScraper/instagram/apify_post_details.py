from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apify_client import ApifyClient
from dotenv import load_dotenv

try:
    from ..utils.build_db_conn import build_db_conn
except Exception:  # pragma: no cover
    from utils.build_db_conn import build_db_conn


load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

PROFILE_DETAILS_ACTOR_ID = os.getenv("APIFY_PROFILE_DETAILS_ACTOR_ID", "shu8hvrXbJbY3Eb9W")
INSTAGRAM_PROFILES_DATA_COLLECTION = os.getenv(
    "INSTAGRAM_PROFILES_DATA_COLLECTION",
    "instagram_profiles_data",
)
RESERVED_INSTAGRAM_PATH_SEGMENTS = {
    "accounts",
    "about",
    "developer",
    "explore",
    "p",
    "reel",
    "reels",
    "stories",
    "tv",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_username(value: Any) -> str | None:
    text = str(value).strip().lstrip("@").strip("/") if value is not None else ""
    if not text:
        return None
    return text.lower()


def _extract_username_from_url(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None

    parts = [part for part in urlparse(text).path.split("/") if part]
    if not parts:
        return None

    candidate = normalize_username(parts[0])
    if candidate is None or candidate in RESERVED_INSTAGRAM_PATH_SEGMENTS:
        return None
    return candidate


def _extract_username_from_item(item: dict[str, Any]) -> str | None:
    username = normalize_username(item.get("username"))
    if username is not None:
        return username

    for key in ("inputUrl", "url"):
        username = _extract_username_from_url(item.get(key))
        if username is not None:
            return username
    return None


def normalize_usernames(usernames: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_username in usernames:
        username = normalize_username(raw_username)
        if username is None or username in seen:
            continue
        seen.add(username)
        normalized.append(username)
    return normalized


@lru_cache(maxsize=1)
def get_actor_client() -> ApifyClient:
    api_key = os.getenv("APIFY_API_KEY")
    if not api_key:
        raise RuntimeError("APIFY_API_KEY is not configured.")
    return ApifyClient(api_key)


def get_instagram_profiles_data_collection() -> Any | None:
    try:
        db = build_db_conn()
        if db is None:
            return None
        return db[INSTAGRAM_PROFILES_DATA_COLLECTION]
    except Exception:
        return None


def ensure_instagram_profiles_data_indexes(collection: Any) -> None:
    try:
        collection.create_index("username", unique=True)
        collection.create_index("updated_at_epoch")
    except Exception:
        pass


def load_instagram_profile_data_from_db(username: str) -> dict[str, Any] | None:
    normalized_username = normalize_username(username)
    if normalized_username is None:
        return None

    collection = get_instagram_profiles_data_collection()
    if collection is None:
        return None

    try:
        document = collection.find_one({"username": normalized_username}, {"_id": 0})
    except Exception:
        return None

    if not isinstance(document, dict):
        return None

    document["username"] = normalized_username
    return document


def save_instagram_profile_item_to_db(
    item: dict[str, Any],
    collection: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("Apify profile item must be an object.")

    normalized_username = _extract_username_from_item(item)
    if normalized_username is None:
        raise ValueError("Apify profile item is missing a valid username.")

    payload = dict(item)
    payload.pop("_id", None)

    updated_at = utc_now_iso()
    payload["username"] = normalized_username
    payload["updated_at"] = updated_at
    payload["updated_at_epoch"] = int(time.time())
    payload.setdefault("scraped_at", updated_at)

    if collection is None:
        collection = get_instagram_profiles_data_collection()
    if collection is None:
        raise RuntimeError(
            "Instagram profiles data database is not available. "
            "Check MongoDB connectivity before scraping."
        )
    ensure_instagram_profiles_data_indexes(collection)

    collection.update_one(
        {"username": normalized_username},
        {"$set": payload},
        upsert=True,
    )
    return payload


def build_run_input(usernames: list[str]) -> dict[str, Any]:
    profile_urls = [f"https://www.instagram.com/{username}/" for username in usernames]
    return {
        "addParentData": False,
        "directUrls": profile_urls,
        "resultsLimit": 24,
        "resultsType": "details",
        "searchLimit": 1,
        "searchType": "hashtag",
    }


def fetch_and_store_profile_data_blocking(usernames: list[str]) -> list[dict[str, Any]]:
    normalized_usernames = normalize_usernames(usernames)
    if not normalized_usernames:
        raise ValueError("Provide at least one valid username.")

    collection = get_instagram_profiles_data_collection()
    if collection is None:
        raise RuntimeError(
            "Instagram profiles data database is not available. "
            "Check MongoDB connectivity before scraping."
        )
    ensure_instagram_profiles_data_indexes(collection)

    actor_client = get_actor_client()
    run = actor_client.actor(PROFILE_DETAILS_ACTOR_ID).call(
        run_input=build_run_input(normalized_usernames)
    )

    saved_items: list[dict[str, Any]] = []
    dataset = actor_client.dataset(run["defaultDatasetId"])
    for item in dataset.iterate_items():
        if not isinstance(item, dict):
            continue
        print(item)
        saved_items.append(save_instagram_profile_item_to_db(item, collection))

    return saved_items


async def get_profile_data(usernames: list[str]) -> list[dict[str, Any]]:
    return await asyncio.to_thread(fetch_and_store_profile_data_blocking, usernames)


if __name__ == "__main__":
    data = asyncio.run(get_profile_data(["thepyromedia"]))
    print(data)
