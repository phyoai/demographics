from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file if present

try:
    from BrightScraper.utils.build_db_conn import build_db_conn
except Exception:  # pragma: no cover
    try:
        from .build_db_conn import build_db_conn
    except Exception:  # pragma: no cover
        from build_db_conn import build_db_conn


DEFAULT_COLLECTION = os.getenv("PROFILES_DB_COLLECTION", "instagram_profiles")
DEFAULT_JSON_PATH = Path(__file__).resolve().parents[1] / "spider_scrapy" / "data" / "profiles.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_username(value: Any) -> str | None:
    text = str(value).strip().lstrip("@").strip("/") if value is not None else ""
    if not text:
        return None
    return text.lower()


def normalize_profile_bundle(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    payload = dict(value)
    profile_section = payload.get("profile")
    posts_section = payload.get("posts")
    if not isinstance(profile_section, dict):
        payload["profile"] = {}
    if not isinstance(posts_section, dict):
        payload["posts"] = {}
    return payload


def load_profiles_json(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError("Invalid JSON root: expected an object.")

    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("Invalid JSON shape: 'profiles' must be an object.")

    normalized_profiles: dict[str, dict[str, Any]] = {}
    for username, profile_bundle in profiles.items():
        normalized_username = normalize_username(username)
        normalized_bundle = normalize_profile_bundle(profile_bundle)
        if normalized_username is None or normalized_bundle is None:
            continue
        normalized_profiles[normalized_username] = normalized_bundle
    return normalized_profiles


def import_profiles_to_db(
    profiles: dict[str, dict[str, Any]],
    collection_name: str,
    limit: int | None = None,
) -> tuple[int, int, list[str]]:
    db = build_db_conn()
    if db is None:
        raise RuntimeError(
            "Database connection failed. Check MONGO_URI / MONGO_DB_NAME and MongoDB availability."
        )

    collection = db[collection_name]
    try:
        collection.create_index("username", unique=True)
        collection.create_index("updated_at_epoch")
    except Exception:
        pass

    saved = 0
    failed = 0
    errors: list[str] = []
    updated_at = utc_now_iso()
    updated_at_epoch = int(time.time())

    for username, profile_bundle in profiles.items():
        if limit is not None and limit > 0 and saved >= limit:
            break

        try:
            collection.update_one(
                {"username": username},
                {
                    "$set": {
                        "username": username,
                        "profile_bundle": profile_bundle,
                        "updated_at": updated_at,
                        "updated_at_epoch": updated_at_epoch,
                    }
                },
                upsert=True,
            )
            saved += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 10:
                errors.append(f"{username}: {exc!r}")

    return saved, failed, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import BrightScraper profiles.json into MongoDB.")
    parser.add_argument(
        "--json-path",
        default=str(DEFAULT_JSON_PATH),
        help="Path to profiles.json (default: BrightScraper/spider_scrapy/data/profiles.json)",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"MongoDB collection name (default: {DEFAULT_COLLECTION})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of profiles to import (0 means all).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    json_path = Path(args.json_path).expanduser().resolve()

    try:
        profiles = load_profiles_json(json_path)
    except Exception as exc:
        print(f"[ERROR] Failed to load profiles JSON: {exc}", file=sys.stderr)
        return 1

    if not profiles:
        print("[INFO] No valid profiles found in JSON. Nothing to import.")
        return 0

    limit = args.limit if isinstance(args.limit, int) and args.limit > 0 else None
    try:
        saved, failed, errors = import_profiles_to_db(
            profiles=profiles,
            collection_name=str(args.collection).strip() or DEFAULT_COLLECTION,
            limit=limit,
        )
    except Exception as exc:
        print(f"[ERROR] Failed to import profiles to MongoDB: {exc}", file=sys.stderr)
        return 1

    print(f"[INFO] JSON file: {json_path}")
    print(f"[INFO] Collection: {args.collection}")
    print(f"[INFO] Total parsed profiles: {len(profiles)}")
    print(f"[INFO] Imported profiles: {saved}")
    print(f"[INFO] Failed profiles: {failed}")
    if errors:
        print("[WARN] Sample errors:")
        for item in errors:
            print(f"  - {item}")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
