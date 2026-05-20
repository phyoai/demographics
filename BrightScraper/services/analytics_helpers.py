from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


def to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        if "." in text:
            return int(float(text))
        return int(text)
    except ValueError:
        return None


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def get_post_comments_count(post: dict[str, Any]) -> int | None:
    comments_count = to_int(post.get("comments_count"))
    if comments_count is not None:
        return comments_count
    comments_count = to_int(post.get("commentsCount"))
    if comments_count is not None:
        return comments_count
    comments = post.get("comments")
    if isinstance(comments, list):
        return len(comments)
    latest_comments = post.get("latestComments")
    if isinstance(latest_comments, list):
        return len(latest_comments)
    return None


def normalize_post_for_analytics(post: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(post, dict):
        return None

    likes = to_int(post.get("likes"))
    if likes is None:
        likes = to_int(post.get("likesCount"))
    comments = get_post_comments_count(post)
    views = to_int(post.get("views"))
    if views is None:
        views = to_int(post.get("videoViewCount"))
    published_at_raw = post.get("published_at")
    if published_at_raw is None:
        published_at_raw = post.get("timestamp")
    scraped_at_raw = post.get("scraped_at")
    if scraped_at_raw is None:
        scraped_at_raw = post.get("updated_at")
    published_at = parse_iso_datetime(published_at_raw)
    scraped_at = parse_iso_datetime(scraped_at_raw)
    effective_dt = published_at or scraped_at
    if effective_dt is None:
        return None

    engagement = (likes or 0) + (comments or 0) + (views or 0)
    return {
        "shortcode": post.get("shortcode") or post.get("shortCode"),
        "post_url": post.get("post_url") or post.get("url"),
        "post_type": post.get("post_type") or post.get("type") or post.get("productType"),
        "published_at": published_at_raw,
        "effective_dt": effective_dt,
        "likes": likes,
        "comments": comments,
        "views": views,
        "engagement": engagement,
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    post_count = len(records)
    likes_values = [record["likes"] for record in records if record.get("likes") is not None]
    comments_values = [record["comments"] for record in records if record.get("comments") is not None]
    views_values = [record["views"] for record in records if record.get("views") is not None]

    total_likes = sum(likes_values)
    total_comments = sum(comments_values)
    total_views = sum(views_values)
    total_engagement = sum((record.get("engagement") or 0) for record in records)

    return {
        "posts": post_count,
        "totals": {
            "likes": total_likes,
            "comments": total_comments,
            "views": total_views,
            "engagement": total_engagement,
        },
        "averages": {
            "likes": round(total_likes / len(likes_values), 2) if likes_values else None,
            "comments": round(total_comments / len(comments_values), 2) if comments_values else None,
            "views": round(total_views / len(views_values), 2) if views_values else None,
            "engagement_per_post": round(total_engagement / post_count, 2) if post_count else None,
        },
        "availability": {
            "likes_posts": len(likes_values),
            "comments_posts": len(comments_values),
            "views_posts": len(views_values),
        },
    }


def week_range_from_iso_year_week(iso_year: int, iso_week: int) -> tuple[str, str]:
    start = date.fromisocalendar(iso_year, iso_week, 1)
    end = date.fromisocalendar(iso_year, iso_week, 7)
    return start.isoformat(), end.isoformat()


def aggregate_by_period(records: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        dt = record["effective_dt"]
        if period == "weekly":
            iso_year, iso_week, _ = dt.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
        elif period == "monthly":
            key = f"{dt.year}-{dt.month:02d}"
        else:
            key = f"{dt.year}"
        grouped.setdefault(key, []).append(record)

    output: list[dict[str, Any]] = []
    for key in sorted(grouped.keys()):
        summary = summarize_records(grouped[key])
        bucket = {
            "period": key,
            **summary,
        }
        if period == "weekly":
            year_text, week_text = key.split("-W")
            start_date, end_date = week_range_from_iso_year_week(int(year_text), int(week_text))
            bucket["start_date"] = start_date
            bucket["end_date"] = end_date
        elif period == "monthly":
            bucket["start_date"] = f"{key}-01"
            bucket["end_date"] = None
        else:
            bucket["start_date"] = f"{key}-01-01"
            bucket["end_date"] = f"{key}-12-31"
        output.append(bucket)
    return output


def _post_rank_key(record: dict[str, Any]) -> tuple[int, int, int, int, float]:
    effective_dt = record.get("effective_dt")
    dt_score = effective_dt.timestamp() if effective_dt is not None else 0.0
    return (
        record.get("engagement") or 0,
        record.get("views") or 0,
        record.get("likes") or 0,
        record.get("comments") or 0,
        dt_score,
    )


def _engagement_rate_percent(engagement: Any, followers: int | None) -> float | None:
    if followers is None or followers <= 0:
        return None
    engagement_value = to_int(engagement)
    if engagement_value is None:
        return None
    return round((engagement_value / followers) * 100, 2)


def _add_engagement_rates_to_summary(summary: dict[str, Any], followers: int | None) -> dict[str, Any]:
    totals = summary.get("totals", {})
    averages = summary.get("averages", {})
    summary["rates"] = {
        "engagement_rate_percent": _engagement_rate_percent(totals.get("engagement"), followers),
        "avg_engagement_rate_percent": _engagement_rate_percent(averages.get("engagement_per_post"), followers),
    }
    return summary


def _post_record_output(record: dict[str, Any], followers: int | None = None) -> dict[str, Any]:
    return {
        "shortcode": record.get("shortcode"),
        "post_url": record.get("post_url"),
        "post_type": record.get("post_type"),
        "published_at": record.get("published_at"),
        "likes": record.get("likes"),
        "comments": record.get("comments"),
        "views": record.get("views"),
        "engagement": record.get("engagement"),
        "engagement_rate_percent": _engagement_rate_percent(record.get("engagement"), followers),
    }


def top_posts(records: list[dict[str, Any]], limit: int = 5, followers: int | None = None) -> list[dict[str, Any]]:
    ranked = sorted(
        records,
        key=_post_rank_key,
        reverse=True,
    )
    output: list[dict[str, Any]] = []
    for record in ranked[:limit]:
        output.append(_post_record_output(record, followers))
    return output


def latest_top_post_by_engagement(
    records: list[dict[str, Any]],
    lookback_days: int = 30,
    followers: int | None = None,
) -> dict[str, Any] | None:
    if not records:
        return None

    latest_dt = max(record["effective_dt"] for record in records)
    window_start = latest_dt - timedelta(days=lookback_days)
    candidates = [record for record in records if record["effective_dt"] >= window_start]
    if not candidates:
        return None

    best = max(candidates, key=_post_rank_key)
    return {
        "window_days": lookback_days,
        "window_start": window_start.date().isoformat(),
        "window_end": latest_dt.date().isoformat(),
        "post": _post_record_output(best, followers),
    }


def build_user_analytics(username: str, profile_bundle: dict[str, Any]) -> dict[str, Any]:
    profile_section: dict[str, Any] = {}
    posts_iterable: list[dict[str, Any]] | Any = []
    total_posts_in_storage = 0

    if isinstance(profile_bundle, dict) and (
        isinstance(profile_bundle.get("profile"), dict) or isinstance(profile_bundle.get("posts"), dict)
    ):
        profile_section = profile_bundle.get("profile", {}) if isinstance(profile_bundle, dict) else {}
        posts_section = profile_bundle.get("posts", {}) if isinstance(profile_bundle, dict) else {}
        posts_iterable = posts_section.values() if isinstance(posts_section, dict) else []
        total_posts_in_storage = len(posts_section) if isinstance(posts_section, dict) else 0
        profile_output = {
            "username": profile_section.get("username"),
            "full_name": profile_section.get("full_name"),
            "followers": to_int(profile_section.get("followers")),
            "following": to_int(profile_section.get("following")),
            "posts": to_int(profile_section.get("posts")),
            "is_verified": profile_section.get("is_verified"),
        }
    else:
        profile_section = profile_bundle if isinstance(profile_bundle, dict) else {}
        latest_posts = profile_section.get("latestPosts")
        posts_iterable = latest_posts if isinstance(latest_posts, list) else []
        total_posts_in_storage = len(posts_iterable)
        profile_output = {
            "username": profile_section.get("username") or username,
            "full_name": profile_section.get("fullName"),
            "followers": to_int(profile_section.get("followersCount")),
            "following": to_int(profile_section.get("followsCount")),
            "posts": to_int(profile_section.get("postsCount")),
            "is_verified": profile_section.get("verified"),
        }

    records: list[dict[str, Any]] = []
    undated_posts = 0
    for post in posts_iterable:
        normalized = normalize_post_for_analytics(post)
        if normalized is None:
            undated_posts += 1
            continue
        records.append(normalized)

    overall = summarize_records(records)
    overall["undated_posts"] = undated_posts
    overall["total_posts_in_storage"] = total_posts_in_storage
    follower_count = to_int(profile_output.get("followers"))
    _add_engagement_rates_to_summary(overall, follower_count)

    weekly = aggregate_by_period(records, "weekly")
    monthly = aggregate_by_period(records, "monthly")
    yearly = aggregate_by_period(records, "yearly")
    for bucket in weekly:
        _add_engagement_rates_to_summary(bucket, follower_count)
    for bucket in monthly:
        _add_engagement_rates_to_summary(bucket, follower_count)
    for bucket in yearly:
        _add_engagement_rates_to_summary(bucket, follower_count)

    return {
        "username": username,
        "profile": profile_output,
        "overall": overall,
        "weekly": weekly,
        "monthly": monthly,
        "yearly": yearly,
        "latest_top_post_by_engagement": latest_top_post_by_engagement(
            records,
            lookback_days=30,
            followers=follower_count,
        ),
        "top_posts_by_engagement": top_posts(records, limit=5, followers=follower_count),
    }
