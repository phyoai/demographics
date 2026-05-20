from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

NOMINATIM_SEARCH_URL = os.getenv(
    "NOMINATIM_SEARCH_URL",
    "https://nominatim.openstreetmap.org/search",
)
NOMINATIM_REVERSE_URL = os.getenv(
    "NOMINATIM_REVERSE_URL",
    "https://nominatim.openstreetmap.org/reverse",
)
NOMINATIM_USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "BrightScraper/2.0 (location lookup service)",
)
NOMINATIM_TIMEOUT_SECONDS = float(os.getenv("NOMINATIM_TIMEOUT_SECONDS", "20"))
DEFAULT_LOCATION_LOOKUP_PARALLELISM = int(os.getenv("LOCATION_LOOKUP_DEFAULT_PARALLELISM", "4"))
MAX_LOCATION_LOOKUP_PARALLELISM = int(os.getenv("LOCATION_LOOKUP_MAX_PARALLELISM", "12"))
DEFAULT_LOCATION_RESULT_LIMIT = int(os.getenv("LOCATION_LOOKUP_DEFAULT_LIMIT", "1"))
MAX_LOCATION_RESULT_LIMIT = int(os.getenv("LOCATION_LOOKUP_MAX_LIMIT", "5"))
DEFAULT_REVERSE_LOOKUP_ZOOM = int(os.getenv("LOCATION_REVERSE_DEFAULT_ZOOM", "15"))
MAX_REVERSE_LOOKUP_ZOOM = int(os.getenv("LOCATION_REVERSE_MAX_ZOOM", "18"))


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_location_query(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_coordinate(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_error_result(
    index: int,
    query: str | None,
    error_message: str,
    started_at: float,
) -> dict[str, Any]:
    return {
        "index": index,
        "success": False,
        "query": query,
        "resolved_query": query,
        "result_count": 0,
        "results": [],
        "error": error_message,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
    }


def build_reverse_error_result(
    index: int,
    lat: float | None,
    lon: float | None,
    zoom: int,
    error_message: str,
    started_at: float,
) -> dict[str, Any]:
    return {
        "index": index,
        "success": False,
        "query": {
            "lat": lat,
            "lon": lon,
            "zoom": zoom,
        },
        "resolved_query": None,
        "result_count": 0,
        "results": [],
        "error": error_message,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
    }


async def fetch_location(
    client: httpx.AsyncClient,
    query: str,
    limit: int = DEFAULT_LOCATION_RESULT_LIMIT,
) -> dict[str, Any]:
    normalized_query = normalize_location_query(query)

    if not normalized_query:
        raise ValueError("search query is required")
    max_results = clamp_int(
        value=limit,
        default=DEFAULT_LOCATION_RESULT_LIMIT,
        minimum=1,
        maximum=MAX_LOCATION_RESULT_LIMIT,
    )

    response = await client.get(
        NOMINATIM_SEARCH_URL,
        params={
            "q": normalized_query,
            "format": "jsonv2",
            "limit": max_results,
        },
    )
    response.raise_for_status()

    payload = response.json()
    results = payload if isinstance(payload, list) else []

    return {
        "query": normalized_query,
        "resolved_query": normalized_query,
        "result_count": len(results),
        "results": results,
    }


async def fetch_location_by_coordinates(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    zoom: int = DEFAULT_REVERSE_LOOKUP_ZOOM,
) -> dict[str, Any]:
    normalized_lat = normalize_coordinate(lat)
    normalized_lon = normalize_coordinate(lon)
    if normalized_lat is None:
        raise ValueError("lat is required")
    if normalized_lon is None:
        raise ValueError("lon is required")
    if normalized_lat < -90 or normalized_lat > 90:
        raise ValueError("lat must be between -90 and 90")
    if normalized_lon < -180 or normalized_lon > 180:
        raise ValueError("lon must be between -180 and 180")

    effective_zoom = clamp_int(
        value=zoom,
        default=DEFAULT_REVERSE_LOOKUP_ZOOM,
        minimum=0,
        maximum=MAX_REVERSE_LOOKUP_ZOOM,
    )

    response = await client.get(
        NOMINATIM_REVERSE_URL,
        params={
            "lat": normalized_lat,
            "lon": normalized_lon,
            "zoom": effective_zoom,
            "format": "jsonv2",
        },
    )
    response.raise_for_status()

    payload = response.json()
    has_result = isinstance(payload, dict) and bool(payload)
    results = [payload] if has_result else []

    return {
        "query": {
            "lat": normalized_lat,
            "lon": normalized_lon,
            "zoom": effective_zoom,
        },
        "resolved_query": f"{normalized_lat},{normalized_lon}",
        "result_count": len(results),
        "results": results,
    }


async def resolve_location_requests(
    queries: list[str],
    parallelism: int = DEFAULT_LOCATION_LOOKUP_PARALLELISM,
    limit: int = DEFAULT_LOCATION_RESULT_LIMIT,
) -> dict[str, Any]:
    started_at = time.monotonic()
    effective_parallelism = clamp_int(
        value=parallelism,
        default=DEFAULT_LOCATION_LOOKUP_PARALLELISM,
        minimum=1,
        maximum=MAX_LOCATION_LOOKUP_PARALLELISM,
    )
    max_results = clamp_int(
        value=limit,
        default=DEFAULT_LOCATION_RESULT_LIMIT,
        minimum=1,
        maximum=MAX_LOCATION_RESULT_LIMIT,
    )

    semaphore = asyncio.Semaphore(effective_parallelism)
    timeout = httpx.Timeout(timeout=NOMINATIM_TIMEOUT_SECONDS)
    headers = {
        "Accept": "application/json",
        "User-Agent": NOMINATIM_USER_AGENT,
    }

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:

        async def process_one(index: int, raw_query: str) -> dict[str, Any]:
            item_started_at = time.monotonic()
            query = normalize_location_query(raw_query)

            if not query:
                return build_error_result(
                    index=index,
                    query=query,
                    error_message="search query is required",
                    started_at=item_started_at,
                )

            try:
                async with semaphore:
                    result = await fetch_location(
                        client=client,
                        query=query,
                        limit=max_results,
                    )
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else "unknown"
                return build_error_result(
                    index=index,
                    query=query,
                    error_message=f"Nominatim returned HTTP {status_code}",
                    started_at=item_started_at,
                )
            except httpx.HTTPError as exc:
                return build_error_result(
                    index=index,
                    query=query,
                    error_message=f"Nominatim request failed: {exc}",
                    started_at=item_started_at,
                )
            except ValueError as exc:
                return build_error_result(
                    index=index,
                    query=query,
                    error_message=str(exc),
                    started_at=item_started_at,
                )
            except Exception as exc:
                return build_error_result(
                    index=index,
                    query=query,
                    error_message=f"Unexpected lookup failure: {exc}",
                    started_at=item_started_at,
                )

            result.update(
                {
                    "index": index,
                    "success": True,
                    "error": None,
                    "elapsed_seconds": round(time.monotonic() - item_started_at, 3),
                }
            )
            return result

        results = await asyncio.gather(*[process_one(index, query) for index, query in enumerate(queries)])

    successful_count = sum(1 for item in results if item.get("success"))
    return {
        "requested_count": len(queries),
        "resolved_count": successful_count,
        "failed_count": len(results) - successful_count,
        "parallelism": effective_parallelism,
        "limit": max_results,
        "timings": {
            "total_seconds": round(time.monotonic() - started_at, 3),
        },
        "results": results,
    }


async def resolve_reverse_location_requests(
    points: list[dict[str, Any]],
    parallelism: int = DEFAULT_LOCATION_LOOKUP_PARALLELISM,
    zoom: int = DEFAULT_REVERSE_LOOKUP_ZOOM,
) -> dict[str, Any]:
    started_at = time.monotonic()
    effective_parallelism = clamp_int(
        value=parallelism,
        default=DEFAULT_LOCATION_LOOKUP_PARALLELISM,
        minimum=1,
        maximum=MAX_LOCATION_LOOKUP_PARALLELISM,
    )
    effective_zoom = clamp_int(
        value=zoom,
        default=DEFAULT_REVERSE_LOOKUP_ZOOM,
        minimum=0,
        maximum=MAX_REVERSE_LOOKUP_ZOOM,
    )

    semaphore = asyncio.Semaphore(effective_parallelism)
    timeout = httpx.Timeout(timeout=NOMINATIM_TIMEOUT_SECONDS)
    headers = {
        "Accept": "application/json",
        "User-Agent": NOMINATIM_USER_AGENT,
    }

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:

        async def process_one(index: int, raw_point: dict[str, Any]) -> dict[str, Any]:
            item_started_at = time.monotonic()
            lat = normalize_coordinate(raw_point.get("lat"))
            lon = normalize_coordinate(raw_point.get("lon"))

            if lat is None:
                return build_reverse_error_result(
                    index=index,
                    lat=lat,
                    lon=lon,
                    zoom=effective_zoom,
                    error_message="lat is required",
                    started_at=item_started_at,
                )
            if lon is None:
                return build_reverse_error_result(
                    index=index,
                    lat=lat,
                    lon=lon,
                    zoom=effective_zoom,
                    error_message="lon is required",
                    started_at=item_started_at,
                )

            try:
                async with semaphore:
                    result = await fetch_location_by_coordinates(
                        client=client,
                        lat=lat,
                        lon=lon,
                        zoom=effective_zoom,
                    )
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else "unknown"
                return build_reverse_error_result(
                    index=index,
                    lat=lat,
                    lon=lon,
                    zoom=effective_zoom,
                    error_message=f"Nominatim returned HTTP {status_code}",
                    started_at=item_started_at,
                )
            except httpx.HTTPError as exc:
                return build_reverse_error_result(
                    index=index,
                    lat=lat,
                    lon=lon,
                    zoom=effective_zoom,
                    error_message=f"Nominatim request failed: {exc}",
                    started_at=item_started_at,
                )
            except ValueError as exc:
                return build_reverse_error_result(
                    index=index,
                    lat=lat,
                    lon=lon,
                    zoom=effective_zoom,
                    error_message=str(exc),
                    started_at=item_started_at,
                )
            except Exception as exc:
                return build_reverse_error_result(
                    index=index,
                    lat=lat,
                    lon=lon,
                    zoom=effective_zoom,
                    error_message=f"Unexpected reverse lookup failure: {exc}",
                    started_at=item_started_at,
                )

            result.update(
                {
                    "index": index,
                    "success": True,
                    "error": None,
                    "elapsed_seconds": round(time.monotonic() - item_started_at, 3),
                }
            )
            return result

        results = await asyncio.gather(*[process_one(index, point) for index, point in enumerate(points)])

    successful_count = sum(1 for item in results if item.get("success"))
    return {
        "requested_count": len(points),
        "resolved_count": successful_count,
        "failed_count": len(results) - successful_count,
        "parallelism": effective_parallelism,
        "zoom": effective_zoom,
        "timings": {
            "total_seconds": round(time.monotonic() - started_at, 3),
        },
        "results": results,
    }
