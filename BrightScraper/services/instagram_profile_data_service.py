from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import HTTPException, status

try:
    from ..instagram.apify_post_details import (
        INSTAGRAM_PROFILES_DATA_COLLECTION,
        fetch_and_store_profile_data_blocking,
        get_instagram_profiles_data_collection,
        load_instagram_profile_data_from_db,
    )
    from .analytics_helpers import parse_iso_datetime, to_int
except ImportError:  # pragma: no cover
    from instagram.apify_post_details import (
        INSTAGRAM_PROFILES_DATA_COLLECTION,
        fetch_and_store_profile_data_blocking,
        get_instagram_profiles_data_collection,
        load_instagram_profile_data_from_db,
    )
    from services.analytics_helpers import parse_iso_datetime, to_int


DEFAULT_INSTAGRAM_PROFILE_DATA_REFRESH_AFTER_SECONDS = int(timedelta(days=7).total_seconds())


def get_instagram_profile_data_age_seconds(document: dict[str, Any]) -> int | None:
    if not isinstance(document, dict):
        return None

    updated_at_epoch = to_int(document.get("updated_at_epoch"))
    if updated_at_epoch is not None and updated_at_epoch > 0:
        return max(0, int(time.time() - updated_at_epoch))

    for key in ("updated_at", "scraped_at"):
        parsed = parse_iso_datetime(document.get(key))
        if parsed is None:
            continue
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))

    return None


def is_instagram_profile_data_stale(
    document: dict[str, Any],
    max_age_seconds: int = DEFAULT_INSTAGRAM_PROFILE_DATA_REFRESH_AFTER_SECONDS,
) -> bool:
    age_seconds = get_instagram_profile_data_age_seconds(document)
    if age_seconds is None:
        return True
    return age_seconds > max_age_seconds


async def resolve_instagram_profile_data_usernames(
    usernames: list[str],
    refresh_stale: bool = False,
    get_collection_fn: Callable[[], Any | None] = get_instagram_profiles_data_collection,
    load_from_db_fn: Callable[[str], dict[str, Any] | None] = load_instagram_profile_data_from_db,
    fetch_and_store_fn: Callable[[list[str]], list[dict[str, Any]]] = fetch_and_store_profile_data_blocking,
    collection_name: str = INSTAGRAM_PROFILES_DATA_COLLECTION,
) -> dict[str, Any]:
    if get_collection_fn() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Instagram profiles data database is not available. "
                "Check MongoDB connectivity before fetching profile data."
            ),
        )

    profiles: dict[str, dict[str, Any]] = {}
    db_usernames: list[str] = []
    stale_usernames: list[str] = []
    fetch_usernames: list[str] = []
    for username in usernames:
        cached_document = load_from_db_fn(username)
        if cached_document is None:
            fetch_usernames.append(username)
            continue
        if refresh_stale and is_instagram_profile_data_stale(cached_document):
            stale_usernames.append(username)
            fetch_usernames.append(username)
            continue
        profiles[username] = {
            "source": "db",
            "data": cached_document,
        }
        db_usernames.append(username)

    scraped_items: list[dict[str, Any]] = []
    fetched_usernames: list[str] = []
    refreshed_usernames: list[str] = []
    if fetch_usernames:
        try:
            scraped_items = await asyncio.to_thread(
                fetch_and_store_fn,
                fetch_usernames,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch Instagram profile data: {exc!r}",
            ) from exc

        for username in fetch_usernames:
            stored_document = load_from_db_fn(username)
            if stored_document is None:
                continue
            profiles[username] = {
                "source": "apify",
                "data": stored_document,
            }
            fetched_usernames.append(username)
            if username in stale_usernames:
                refreshed_usernames.append(username)

    not_found_usernames = [
        username for username in usernames if username not in profiles
    ]
    return {
        "collection": collection_name,
        "requested_usernames": usernames,
        "db_usernames": db_usernames,
        "stale_usernames": stale_usernames,
        "fetched_usernames": fetched_usernames,
        "refreshed_usernames": refreshed_usernames,
        "not_found_usernames": not_found_usernames,
        "fetched_count": len(scraped_items),
        "profiles": profiles,
    }
