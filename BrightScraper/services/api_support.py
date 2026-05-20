from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, status

try:
    import logfire
except Exception:  # pragma: no cover
    logfire = None

try:
    from ..api_models import ReverseLocationPoint
    from ..instagram.apify_post_details import normalize_username as normalize_instagram_username
except ImportError:  # pragma: no cover
    from api_models import ReverseLocationPoint
    from instagram.apify_post_details import normalize_username as normalize_instagram_username


load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

DEFAULT_ANALYZE_DEADLINE_SECONDS = int(os.getenv("ANALYZE_DEFAULT_DEADLINE_SECONDS", "30"))
MIN_ANALYZE_DEADLINE_SECONDS = int(os.getenv("ANALYZE_MIN_DEADLINE_SECONDS", "10"))
MAX_ANALYZE_DEADLINE_SECONDS = int(os.getenv("ANALYZE_MAX_DEADLINE_SECONDS", "120"))


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def normalize_deadline_seconds(value: Any) -> int:
    if value is None:
        return DEFAULT_ANALYZE_DEADLINE_SECONDS
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ANALYZE_DEADLINE_SECONDS
    return max(MIN_ANALYZE_DEADLINE_SECONDS, min(MAX_ANALYZE_DEADLINE_SECONDS, number))


def configure_logfire_for_app(api_app: FastAPI) -> None:
    api_key = os.getenv("LOGFIRE_API_KEY")
    if (
        logfire is None
        or parse_bool(os.getenv("LOGFIRE_DISABLED"), False)
        or not api_key
    ):
        return

    configure_kwargs: dict[str, Any] = {
        "environment": os.getenv("LOGFIRE_ENVIRONMENT", "production"),
        "send_to_logfire": "if-token-present",
        "min_level": [
            "trace",
            "debug",
            "info",
            "notice",
            "warn",
            "warning",
            "error",
            "fatal",
        ],
        "api_key": api_key,
    }

    try:
        logfire.configure(**configure_kwargs)
        logfire.instrument_fastapi(api_app, capture_headers=True)
    except Exception as exc:  # pragma: no cover
        print(f"[startup] Logfire disabled: {exc}")


def verify_api_key(api_key: str | None = Header(default=None, alias="api-key")) -> bool:
    expected_key = os.getenv("PHYO_SERVER_API_KEY")

    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server API key is not configured",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    if api_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return True


def normalize_input_usernames(username: str | None, usernames: list[str]) -> list[str]:
    values: list[str] = []
    if username:
        values.append(username)
    values.extend(usernames)

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        candidate = normalize_instagram_username(raw_value)
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def normalize_location_queries(
    search_query: str | None,
    search_queries: list[str],
) -> list[str]:
    values: list[str] = []
    if search_query:
        values.append(search_query)
    values.extend(search_queries)

    normalized: list[str] = []
    for value in values:
        query = str(value).strip()
        if query:
            normalized.append(query)
    return normalized


def normalize_reverse_location_points(
    point: ReverseLocationPoint | None,
    points: list[ReverseLocationPoint],
) -> list[dict[str, float]]:
    values: list[ReverseLocationPoint] = []
    if point is not None:
        values.append(point)
    values.extend(points)

    normalized: list[dict[str, float]] = []
    for value in values:
        normalized.append(
            {
                "lat": float(value.lat),
                "lon": float(value.lon),
            }
        )
    return normalized
