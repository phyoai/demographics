from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    import pymongo
except Exception:  # pragma: no cover
    pymongo = None  # type: ignore[assignment]

try:
    from ..audience_analytics import AnalysisDeadlineExceeded, AudienceAnalytics
    from ..brightdata_client import (
        BrightDataBadResponseError,
        BrightDataRateLimitError,
        BrightDataTimeoutError,
    )
    from ..enhanced_response_formatter import EnhancedResponseFormatter
    from ..utils.age_gropu_fixer import redistribute_to_zero_groups
    from ..utils.build_db_conn import build_db_conn
except ImportError:  # pragma: no cover
    from audience_analytics import AnalysisDeadlineExceeded, AudienceAnalytics
    from brightdata_client import (
        BrightDataBadResponseError,
        BrightDataRateLimitError,
        BrightDataTimeoutError,
    )
    from enhanced_response_formatter import EnhancedResponseFormatter
    from utils.age_gropu_fixer import redistribute_to_zero_groups

    try:
        from utils.build_db_conn import build_db_conn
    except Exception:  # pragma: no cover
        build_db_conn = None


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

ANALYZE_CACHE_DIR = PROJECT_ROOT / "api_cache"
ANALYZE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
ANALYZE_JOB_TTL_SECONDS = int(os.getenv("ANALYZE_JOB_TTL_SECONDS", "3600"))
ANALYZE_CACHE_DB_COLLECTION = os.getenv("ANALYZE_CACHE_DB_COLLECTION", "demographics_cache")
STORED_SCRAPES_MONGO_URI = os.getenv("STORED_SCRAPES_MONGO_URI") or os.getenv("MONGO_URI", "mongodb://localhost:27017")
STORED_SCRAPES_DB_NAME = os.getenv("STORED_SCRAPES_DB_NAME", "instagpy")
STORED_SCRAPES_COLLECTION = os.getenv("STORED_SCRAPES_COLLECTION", "instagram_scrapes")

response_formatter = EnhancedResponseFormatter()


class StoredDataAudienceAnalytics(AudienceAnalytics):
    """AudienceAnalytics adapter that uses a previously stored scrape."""

    def __init__(self, profile: dict[str, Any], posts: list[dict[str, Any]], comments: list[dict[str, Any]]):
        super().__init__(use_ai=False)
        self._stored_profile = profile
        self._stored_posts = posts
        self._stored_comments = comments

    def scrape_profile_and_posts(
        self,
        username: str,
        *,
        retry_summary: dict[str, Any] | None = None,
        deadline_at: float | None = None,
    ) -> dict[str, Any]:
        return self._stored_profile

    def scrape_all_comments(
        self,
        posts: list[dict[str, Any]],
        *,
        max_posts: int = 6,
        followers: int = 0,
        retry_summary: dict[str, Any] | None = None,
        deadline_at: float | None = None,
        fast_mode: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._stored_comments, {
            "status": "success",
            "warnings": [],
            "posts_scraped": len(self._stored_posts),
            "posts_considered": len(self._stored_posts),
            "target_comments": len(self._stored_comments),
            "collected_comments": len(self._stored_comments),
            "source": "stored_data",
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_analytics_engine() -> AudienceAnalytics:
    return AudienceAnalytics()


def get_analyze_db_collection() -> Any | None:
    if build_db_conn is None:
        return None

    try:
        db = build_db_conn()
        if db is None:
            return None
        return db[ANALYZE_CACHE_DB_COLLECTION]
    except Exception:
        return None


def get_stored_scrapes_collection() -> Any | None:
    if pymongo is None:
        return None

    try:
        client = pymongo.MongoClient(STORED_SCRAPES_MONGO_URI, serverSelectionTimeoutMS=2000)
        client.admin.command("ping")
        return client[STORED_SCRAPES_DB_NAME][STORED_SCRAPES_COLLECTION]
    except Exception:
        logger.exception("Failed to connect to stored Instagram scrape MongoDB collection")
        return None


def load_stored_instagram_scrape(username: str) -> dict[str, Any] | None:
    collection = get_stored_scrapes_collection()
    if collection is None:
        return None

    username_pattern = re.compile(f"^{re.escape(username)}$", re.IGNORECASE)
    query = {
        "$or": [
            {"requested_username": username_pattern},
            {"username": username_pattern},
            {"result.requested_username": username_pattern},
            {"result.profile.username": username_pattern},
        ]
    }

    try:
        return collection.find_one(
            query,
            sort=[
                ("scraped_at", -1),
                ("created_at", -1),
                ("_id", -1),
            ],
        )
    except Exception:
        logger.exception("Failed to load stored Instagram scrape for @%s", username)
        return None


def coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def normalize_stored_comment(comment: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(comment)
    normalized["username"] = str(normalized.get("username") or "").strip()
    normalized["text"] = str(normalized.get("text") or "")
    normalized["timestamp"] = (
        normalized.get("timestamp")
        or normalized.get("posted_at")
        or normalized.get("created_at")
        or normalized.get("posted_at_text")
    )
    normalized["likes"] = coerce_int(normalized.get("likes", normalized.get("likes_count")), 0)
    normalized["likes_count"] = normalized["likes"]
    normalized["post_url"] = post.get("post_url") or post.get("url")
    normalized["post_shortcode"] = post.get("shortcode")
    return normalized


def normalize_stored_post(post: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(post)
    post_url = normalized.get("post_url") or normalized.get("url")
    post_type = normalized.get("post_type") or normalized.get("media_type") or "post"
    comments_count = coerce_int(
        normalized.get("comments_count")
        or normalized.get("comments_count_total")
        or normalized.get("comments_collected_count"),
        0,
    )
    normalized["url"] = post_url
    normalized["post_url"] = post_url
    normalized["media_type"] = str(post_type).lower()
    normalized["likes_count"] = coerce_int(normalized.get("likes_count", normalized.get("likes")), 0)
    normalized["comments_count"] = comments_count
    normalized["timestamp"] = normalized.get("timestamp") or normalized.get("posted_at")
    return normalized


def build_stored_analysis_inputs(
    scrape_doc: dict[str, Any],
    username: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    scrape_result = scrape_doc.get("result") if isinstance(scrape_doc.get("result"), dict) else scrape_doc
    if not isinstance(scrape_result, dict):
        raise ValueError(f"No stored Instagram scrape found for @{username}")

    stored_profile = scrape_result.get("profile")
    if not isinstance(stored_profile, dict):
        raise ValueError(f"No stored Instagram profile data found for @{username}")

    raw_posts = scrape_result.get("posts")
    if not isinstance(raw_posts, list):
        raw_posts = stored_profile.get("recent_posts") if isinstance(stored_profile.get("recent_posts"), list) else []

    posts: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    total_post_comment_slots = 0
    for raw_post in raw_posts:
        if not isinstance(raw_post, dict):
            continue
        post = normalize_stored_post(raw_post)
        posts.append(post)
        raw_comments = raw_post.get("comments", [])
        if not isinstance(raw_comments, list):
            continue
        total_post_comment_slots += len(raw_comments)
        for raw_comment in raw_comments:
            if isinstance(raw_comment, dict):
                comments.append(normalize_stored_comment(raw_comment, post))

    followers = coerce_int(stored_profile.get("followers", stored_profile.get("followers_count")), 0)
    following = coerce_int(stored_profile.get("following", stored_profile.get("following_count")), 0)
    likes_total = sum(coerce_int(post.get("likes_count"), 0) for post in posts)
    comments_total = sum(coerce_int(post.get("comments_count"), 0) for post in posts)
    avg_engagement = 0.0
    if followers > 0 and posts:
        avg_engagement = (likes_total + comments_total) / (followers * len(posts))

    profile = dict(stored_profile)
    profile.update(
        {
            "username": stored_profile.get("username") or username,
            "user_name": stored_profile.get("username") or username,
            "profile_name": stored_profile.get("full_name") or stored_profile.get("profile_name") or "",
            "full_name": stored_profile.get("full_name") or stored_profile.get("profile_name") or "",
            "profile_pic_url": stored_profile.get("profile_pic_url")
            or stored_profile.get("profile_image_link")
            or stored_profile.get("profile_image")
            or "",
            "followers": followers,
            "following": following,
            "posts_count": coerce_int(stored_profile.get("posts_count"), len(posts)),
            "biography": stored_profile.get("biography") or stored_profile.get("bio") or "",
            "is_verified": bool(stored_profile.get("is_verified", False)),
            "is_business_account": bool(
                stored_profile.get("is_business_account")
                or stored_profile.get("is_business")
                or stored_profile.get("business_or_creator_label")
            ),
            "avg_engagement": avg_engagement,
            "posts": posts,
        }
    )

    scrape_id = scrape_doc.get("_id")
    meta = {
        "db": STORED_SCRAPES_DB_NAME,
        "collection": STORED_SCRAPES_COLLECTION,
        "storage_id": str(scrape_id) if scrape_id is not None else scrape_result.get("storage_id"),
        "requested_username": scrape_doc.get("requested_username") or scrape_result.get("requested_username"),
        "scraped_at": str(scrape_doc.get("scraped_at") or scrape_result.get("scraped_at") or ""),
        "posts_loaded": len(posts),
        "posts_used_for_comments": 48,
        "comments_loaded": len(comments),
        "stored_comment_slots": total_post_comment_slots,
        "max_posts_requested": scrape_doc.get("max_posts_requested") or scrape_result.get("max_posts_requested"),
        "max_comments_requested": scrape_doc.get("max_comments_requested") or scrape_result.get("max_comments_requested"),
    }
    return profile, posts, comments, meta


def load_analyze_cache_from_db(username: str) -> tuple[dict[str, Any] | None, int | None]:
    collection = get_analyze_db_collection()
    if collection is None:
        return None, None

    try:
        cache_doc = collection.find_one(
            {"username": username},
            {"payload": 1, "updated_at_epoch": 1},
        )
    except Exception:
        return None, None

    if not isinstance(cache_doc, dict):
        return None, None

    payload = cache_doc.get("payload")
    if not isinstance(payload, dict):
        return None, None

    try:
        updated_at_epoch = int(cache_doc.get("updated_at_epoch", 0))
    except (TypeError, ValueError):
        updated_at_epoch = 0

    age_seconds = max(0, int(time.time() - updated_at_epoch))
    if updated_at_epoch <= 0 or age_seconds > ANALYZE_CACHE_TTL_SECONDS:
        try:
            collection.delete_one({"username": username})
        except Exception:
            pass
        return None, None

    return payload, age_seconds


def save_analyze_cache_to_db(username: str, payload: dict[str, Any]) -> bool:
    collection = get_analyze_db_collection()
    if collection is None:
        return False

    try:
        collection.update_one(
            {"username": username},
            {
                "$set": {
                    "username": username,
                    "payload": payload,
                    "updated_at": utc_now_iso(),
                    "updated_at_epoch": int(time.time()),
                }
            },
            upsert=True,
        )
        return True
    except Exception:
        return False


def get_analyze_cache_path(username: str) -> Path:
    safe_username = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in username.lower())
    return ANALYZE_CACHE_DIR / f"analyze_{safe_username}.json"


def load_analyze_cache(username: str) -> tuple[dict[str, Any] | None, int | None]:
    cache_file = get_analyze_cache_path(username)
    if not cache_file.exists():
        return None, None

    try:
        age_seconds = int(time.time() - cache_file.stat().st_mtime)
        if age_seconds > ANALYZE_CACHE_TTL_SECONDS:
            cache_file.unlink(missing_ok=True)
            return None, None
        with cache_file.open("r", encoding="utf-8") as file_handle:
            return json.load(file_handle), age_seconds
    except Exception:
        return None, None


def save_analyze_cache(username: str, payload: dict[str, Any]) -> str | None:
    try:
        ANALYZE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = get_analyze_cache_path(username)
        with cache_file.open("w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, ensure_ascii=False, indent=2)
        return str(cache_file)
    except Exception:
        return None


def classify_analysis_error(exc: Exception) -> tuple[str, int, str, dict[str, Any]]:
    retry_summary = getattr(exc, "retry_summary", {})
    message = str(exc)
    message_lower = message.lower()

    if isinstance(exc, (BrightDataTimeoutError, AnalysisDeadlineExceeded)):
        return "UPSTREAM_TIMEOUT", 504, message, retry_summary
    if isinstance(exc, BrightDataRateLimitError):
        return "UPSTREAM_RATE_LIMIT", 429, message, retry_summary
    if isinstance(exc, BrightDataBadResponseError):
        return "UPSTREAM_BAD_RESPONSE", 502, message, retry_summary
    if isinstance(exc, ValueError):
        if (
            "private" in message_lower
            or "does not exist" in message_lower
            or "no profile data found" in message_lower
            or "no stored instagram" in message_lower
        ):
            return "NO_DATA", 404, message, retry_summary
        return "INPUT_INVALID", 400, message, retry_summary
    return "INTERNAL_ERROR", 500, message, retry_summary


def build_error_envelope(exc: Exception, username: str | None = None) -> tuple[dict[str, Any], int]:
    error_code, http_status, message, retry_summary = classify_analysis_error(exc)
    return (
        {
            "success": False,
            "status": "timeout" if error_code == "UPSTREAM_TIMEOUT" else "failed",
            "error_code": error_code,
            "error": message,
            "message": f"Failed to analyze @{username}: {message}" if username else message,
            "warnings": [],
            "timings": {},
            "retry_summary": retry_summary or {},
        },
        http_status,
    )


def execute_analysis_pipeline(
    username: str,
    max_posts: int,
    deadline_seconds: int,
    fast_mode: bool,
) -> tuple[dict[str, Any], int]:
    started_at = time.monotonic()
    engine = create_analytics_engine()
    raw_result = engine.analyze_audience(
        username,
        max_posts=max_posts,
        deadline_seconds=deadline_seconds,
        fast_mode=fast_mode,
    )

    enhanced_result = response_formatter.format_enhanced_response(
        profile_data=raw_result,
        demographics=raw_result,
        comments=raw_result.get("comments", []),
    )
    analytics = enhanced_result.get("analytics")
    if isinstance(analytics, dict) and isinstance(analytics.get("ageRange"), dict):
        analytics["ageRange"] = redistribute_to_zero_groups(analytics["ageRange"])

    analysis_status = raw_result.get("analysis_status", "success")
    timings = dict(raw_result.get("timings", {}))
    if "total_seconds" not in timings:
        timings["total_seconds"] = round(time.monotonic() - started_at, 3)

    envelope = {
        "success": analysis_status in {"success", "partial"},
        "status": analysis_status,
        "error_code": None,
        "warnings": raw_result.get("warnings", []),
        "timings": timings,
        "retry_summary": raw_result.get("retry_summary", {}),
        "data": enhanced_result,
        "saved_to_file": None,
    }
    return envelope, 200


def execute_stored_analysis_pipeline(
    username: str,
    deadline_seconds: int,
    fast_mode: bool,
) -> tuple[dict[str, Any], int]:
    started_at = time.monotonic()
    scrape_doc = load_stored_instagram_scrape(username)
    if scrape_doc is None:
        raise ValueError(f"No stored Instagram scrape found for @{username}")

    profile, posts, comments, stored_meta = build_stored_analysis_inputs(scrape_doc, username)
    if not posts and not comments:
        raise ValueError(f"No stored Instagram posts or comments found for @{username}")

    engine = StoredDataAudienceAnalytics(profile=profile, posts=posts, comments=comments)
    raw_result = engine.analyze_audience(
        username,
        max_posts=max(1, len(posts)),
        deadline_seconds=deadline_seconds,
        fast_mode=fast_mode,
        limit_age_source=False,
    )

    enhanced_result = response_formatter.format_enhanced_response(
        profile_data=raw_result,
        demographics=raw_result,
        comments=raw_result.get("comments", []),
    )
    analytics = enhanced_result.get("analytics")
    if isinstance(analytics, dict) and isinstance(analytics.get("ageRange"), dict):
        analytics["ageRange"] = redistribute_to_zero_groups(analytics["ageRange"])

    timings = dict(raw_result.get("timings", {}))
    timings["stored_data_pipeline_seconds"] = round(time.monotonic() - started_at, 3)
    timings["total_seconds"] = timings["stored_data_pipeline_seconds"]

    envelope = {
        "success": raw_result.get("analysis_status", "success") in {"success", "partial"},
        "status": raw_result.get("analysis_status", "success"),
        "error_code": None,
        "warnings": raw_result.get("warnings", []),
        "timings": timings,
        "retry_summary": raw_result.get("retry_summary", {}),
        "data": enhanced_result,
        "stored_data": {
            **stored_meta,
            "used_all_posts": True,
            "used_all_stored_comments": True,
        },
        "saved_to_file": None,
    }
    return envelope, 200


def cleanup_expired_analyze_jobs(app_state: Any) -> None:
    now_epoch = time.time()
    stale_ids: list[str] = []

    jobs = getattr(app_state, "analyze_jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_id, job in jobs.items():
        expires_at = job.get("expires_at_epoch", 0)
        if expires_at and now_epoch >= expires_at:
            stale_ids.append(job_id)

    for job_id in stale_ids:
        app_state.analyze_jobs.pop(job_id, None)
        app_state.analyze_tasks.pop(job_id, None)


def create_analyze_job(app_state: Any, payload: dict[str, Any]) -> dict[str, Any]:
    cleanup_expired_analyze_jobs(app_state)
    now_epoch = time.time()
    job_id = uuid.uuid4().hex
    record = {
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "created_at_epoch": now_epoch,
        "updated_at_epoch": now_epoch,
        "expires_at_epoch": now_epoch + ANALYZE_JOB_TTL_SECONDS,
        "request": payload,
        "result": None,
        "http_status": None,
        "error_code": None,
    }
    app_state.analyze_jobs[job_id] = record
    return record


def update_analyze_job(app_state: Any, job_id: str, **fields: Any) -> dict[str, Any] | None:
    job = app_state.analyze_jobs.get(job_id)
    if not isinstance(job, dict):
        return None

    job.update(fields)
    now_epoch = time.time()
    job["updated_at"] = utc_now_iso()
    job["updated_at_epoch"] = now_epoch
    job["expires_at_epoch"] = now_epoch + ANALYZE_JOB_TTL_SECONDS
    return dict(job)


def get_analyze_job(app_state: Any, job_id: str) -> dict[str, Any] | None:
    cleanup_expired_analyze_jobs(app_state)
    job = app_state.analyze_jobs.get(job_id)
    return dict(job) if isinstance(job, dict) else None


def serialize_analyze_job_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "stage": job.get("stage"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "expires_in_seconds": max(0, int(job.get("expires_at_epoch", 0) - time.time())),
        "error_code": job.get("error_code"),
        "http_status": job.get("http_status"),
        "result": job.get("result"),
    }


async def run_analyze_job(
    app_state: Any,
    job_id: str,
    username: str,
    max_posts: int,
    deadline_seconds: int,
    fast_mode: bool,
    use_stored_data: bool = False,
) -> None:
    update_analyze_job(app_state, job_id, status="running", stage="analysis")

    try:
        if use_stored_data:
            result, http_status = await asyncio.to_thread(
                execute_stored_analysis_pipeline,
                username,
                deadline_seconds,
                fast_mode,
            )
        else:
            result, http_status = await asyncio.to_thread(
                execute_analysis_pipeline,
                username,
                max_posts,
                deadline_seconds,
                fast_mode,
            )
            save_analyze_cache_to_db(username, result)
            save_analyze_cache(username, result)
        update_analyze_job(
            app_state,
            job_id,
            status=result.get("status", "success"),
            stage="completed",
            result=result,
            http_status=http_status,
            error_code=result.get("error_code"),
        )
    except Exception as exc:
        error_response, http_status = build_error_envelope(exc, username=username)
        final_status = "timeout" if error_response.get("error_code") == "UPSTREAM_TIMEOUT" else "failed"
        update_analyze_job(
            app_state,
            job_id,
            status=final_status,
            stage="completed",
            result=error_response,
            http_status=http_status,
            error_code=error_response.get("error_code"),
        )
    finally:
        app_state.analyze_tasks.pop(job_id, None)
