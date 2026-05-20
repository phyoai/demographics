"""
BrightData Instagram comments scraper.

Uses the shared BrightData client for:
- pooled HTTP connections
- balanced retries with jitter
- consistent snapshot polling statuses
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

try:
    from .brightdata_client import BrightDataClient, new_retry_summary
except ImportError:
    from brightdata_client import BrightDataClient, new_retry_summary

load_dotenv()

BRIGHTDATA_COMMENTS_DATASET = os.getenv("BRIGHTDATA_COMMENTS_DATASET_ID", "gd_ltppn085pokosxh13")
MAX_COMMENTS_PER_POST = 200


_DEFAULT_CLIENT: Optional[BrightDataClient] = None


def _get_default_client() -> BrightDataClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = BrightDataClient(retry_profile="balanced")
    return _DEFAULT_CLIENT


def extract_post_code(post_url: str) -> str:
    """Extract Instagram post code from URL."""
    post_url = post_url.rstrip("/")
    if "/p/" in post_url:
        return post_url.split("/p/")[-1].split("/")[0]
    if "/reel/" in post_url:
        return post_url.split("/reel/")[-1].split("/")[0]
    if "/tv/" in post_url:
        return post_url.split("/tv/")[-1].split("/")[0]
    return post_url.split("/")[-1]


def trigger_comments_snapshot(
    post_url: str,
    *,
    client: Optional[BrightDataClient] = None,
    retry_summary: Optional[Dict[str, Any]] = None,
    deadline_at: Optional[float] = None,
) -> Optional[str]:
    """Trigger comments snapshot for a post."""
    client = client or _get_default_client()
    retry_summary = retry_summary or new_retry_summary(profile=client.retry_profile)
    post_code = extract_post_code(post_url)
    print(f"  Triggering BrightData snapshot for post: {post_code}")

    try:
        snapshot_id = client.trigger_snapshot(
            dataset_id=BRIGHTDATA_COMMENTS_DATASET,
            payload=[{"url": post_url}],
            include_errors=True,
            summary=retry_summary,
            deadline_at=deadline_at,
            operation="comments_trigger",
        )
        print(f"  Snapshot triggered: {snapshot_id}")
        return snapshot_id
    except Exception as exc:
        print(f"  Error triggering comments snapshot: {exc}")
        return None


def get_snapshot_data(
    snapshot_id: str,
    *,
    client: Optional[BrightDataClient] = None,
    retry_summary: Optional[Dict[str, Any]] = None,
    deadline_at: Optional[float] = None,
    max_retries: Optional[int] = None,
    retry_delay: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Fetch BrightData snapshot payload."""
    if not snapshot_id:
        return []

    client = client or _get_default_client()
    retry_summary = retry_summary or new_retry_summary(profile=client.retry_profile)

    try:
        data = client.fetch_snapshot_data(
            snapshot_id,
            summary=retry_summary,
            deadline_at=deadline_at,
            poll_retries=max_retries,
            poll_delay=retry_delay,
            operation="comments_snapshot_poll",
        )
        if isinstance(data, list):
            if data:
                print(f"  Got {len(data)} comments from BrightData")
            return data
        return []
    except Exception as exc:
        print(f"  Error polling comments snapshot: {exc}")
        return []


def scrape_comments_brightdata(
    post_url: str,
    max_comments: Optional[int] = None,
    *,
    client: Optional[BrightDataClient] = None,
    retry_summary: Optional[Dict[str, Any]] = None,
    deadline_at: Optional[float] = None,
    max_poll_retries: Optional[int] = None,
    poll_retry_delay: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Scrape comments from an Instagram post using BrightData.

    Returns:
        List[dict]: [{'username', 'text', 'timestamp', 'post_url', ...}]
    """
    if max_comments is None:
        max_comments = MAX_COMMENTS_PER_POST

    client = client or _get_default_client()
    retry_summary = retry_summary or new_retry_summary(profile=client.retry_profile)

    print(f"BrightData Comments Scraper: {post_url}")

    snapshot_id = trigger_comments_snapshot(
        post_url,
        client=client,
        retry_summary=retry_summary,
        deadline_at=deadline_at,
    )
    if not snapshot_id:
        return []

    raw_comments = get_snapshot_data(
        snapshot_id,
        client=client,
        retry_summary=retry_summary,
        deadline_at=deadline_at,
        max_retries=max_poll_retries,
        retry_delay=poll_retry_delay,
    )
    if not raw_comments:
        print("  No comments retrieved from BrightData")
        return []

    formatted_comments: List[Dict[str, Any]] = []

    for idx, comment in enumerate(raw_comments[:max_comments]):
        try:
            if idx < 2:
                print(f"  DEBUG: Raw comment {idx} keys: {list(comment.keys())}")

            author = comment.get("author") or {}
            comment_user = comment.get("comment_user")
            comment_user_url = comment.get("comment_user_url") or ""

            if isinstance(comment_user, dict):
                comment_user_name = (
                    comment_user.get("username")
                    or comment_user.get("user_name")
                    or comment_user.get("handle")
                )
                comment_user_full_name = (
                    comment_user.get("full_name")
                    or comment_user.get("name")
                    or ""
                )
                comment_user_pic = (
                    comment_user.get("profile_pic_url")
                    or comment_user.get("profile_image_link")
                    or ""
                )
            else:
                comment_user_name = comment_user if isinstance(comment_user, str) else ""
                comment_user_full_name = ""
                comment_user_pic = ""

            if not comment_user_name and comment_user_url:
                try:
                    parsed = urlparse(comment_user_url)
                    parts = [part for part in parsed.path.split("/") if part]
                    comment_user_name = parts[0] if parts else ""
                except Exception:
                    comment_user_name = ""

            if isinstance(comment_user_name, str) and comment_user_name.startswith("@"):
                comment_user_name = comment_user_name[1:]

            full_name = (
                comment.get("name")
                or comment.get("full_name")
                or comment_user_full_name
                or (author.get("name") if isinstance(author, dict) else None)
                or ""
            )
            username = (
                comment.get("username")
                or comment_user_name
                or (author.get("username") if isinstance(author, dict) else None)
                or "unknown"
            )

            if idx < 2:
                print(f"    username: {username}, full_name: {full_name}")

            formatted = {
                "username": username,
                "text": (
                    comment.get("text")
                    or comment.get("content")
                    or comment.get("comment")
                    or ""
                ),
                "timestamp": (
                    comment.get("timestamp")
                    or comment.get("created_at")
                    or comment.get("comment_date")
                    or ""
                ),
                "post_url": post_url,
                "full_name": full_name,
                "profile_pic_url": (
                    comment.get("profile_pic_url")
                    or comment_user_pic
                    or (author.get("profile_pic_url") if isinstance(author, dict) else None)
                    or ""
                ),
                "is_bot": comment.get("is_bot", False),
            }

            if formatted["text"] and formatted["text"].strip():
                formatted_comments.append(formatted)
        except Exception as exc:
            print(f"  Error formatting comment: {exc}")

    print(f"  Formatted {len(formatted_comments)} comments")
    return formatted_comments
