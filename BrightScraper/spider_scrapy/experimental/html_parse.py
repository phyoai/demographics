from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import unescape as html_unescape
from pathlib import Path
from typing import Any

from lxml import html


DEFAULT_HTML_FILE = Path("insta_dump/page.html")
DEFAULT_OUTPUT_FILE = Path("insta_dump/commenter_insights.json")

USERNAME_PATH_RE = re.compile(r"^/([A-Za-z0-9._]+)/$")
COMMENT_LINK_RE = re.compile(r"^/p/[A-Za-z0-9_-]+/c/(\d+)/$")
LIKES_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s+likes?", re.IGNORECASE)
REPLY_COUNT_RE = re.compile(r"view(?: all)?\s+(\d+)\s+repl", re.IGNORECASE)
MENTION_RE = re.compile(r"@([A-Za-z0-9._]+)")
RELATIVE_TIME_RE = re.compile(r"^\d+\s*(?:s|m|h|d|w|mo|y)(?:\s+ago)?$", re.IGNORECASE)
COMPACT_NUMBER_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*([kmbt]?)", re.IGNORECASE)
HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
ESCAPED_HTTP_URL_RE = re.compile(r"https?:\\\\/\\\\/[^\s\"'<>]+", re.IGNORECASE)

IGNORED_TOKENS = {
    "like",
    "reply",
    "see translation",
    "view replies",
    "view more replies",
    "view all replies",
    "show more",
    "hide",
    "report",
    "delete",
    "edited",
    "follow",
    "following",
    "verified",
    "audio is muted",
    "original audio",
    "pinned",
    "add a comment...",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def parse_compact_number(value: Any) -> int | None:
    text = normalize_text(value)
    if not text:
        return None
    compact = text.replace(",", "")
    match = COMPACT_NUMBER_RE.search(compact)
    if not match:
        return None
    number_part = match.group(1).replace(",", "")
    suffix = match.group(2).lower()
    multipliers = {
        "": 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "t": 1_000_000_000_000,
    }
    try:
        return int(float(number_part) * multipliers.get(suffix, 1))
    except ValueError:
        return None


def to_iso_datetime(value: str | None) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return None


def parse_post_meta(root) -> dict[str, Any]:
    canonical_url = normalize_text(root.xpath("string(//link[@rel='canonical']/@href)"))
    og_description = normalize_text(
        root.xpath("string(//meta[@property='og:description']/@content)")
        or root.xpath("string(//meta[@name='description']/@content)")
    )
    og_title = normalize_text(root.xpath("string(//meta[@property='og:title']/@content)"))

    likes_text = comments_text = owner_username = None
    if og_description:
        match = re.search(
            r"(?P<likes>[\d,\.KMBTkmbt]+)\s+likes,\s+(?P<comments>[\d,\.KMBTkmbt]+)\s+comments?\s*-\s*(?P<owner>[A-Za-z0-9._]+)",
            og_description,
        )
        if match:
            likes_text = normalize_text(match.group("likes"))
            comments_text = normalize_text(match.group("comments"))
            owner_username = normalize_text(match.group("owner"))

    return {
        "canonical_url": canonical_url,
        "og_title": og_title,
        "og_description": og_description,
        "owner_username": owner_username,
        "likes_text": likes_text,
        "likes": parse_compact_number(likes_text),
        "comments_count_text": comments_text,
        "comments_count": parse_compact_number(comments_text),
    }


def extract_username_from_row(row) -> str | None:
    for anchor in row.xpath(".//a[@href]"):
        href = normalize_text(anchor.get("href"))
        if not href:
            continue
        if COMMENT_LINK_RE.match(href):
            continue
        username_match = USERNAME_PATH_RE.match(href)
        if not username_match:
            continue
        username = normalize_text(username_match.group(1))
        if username:
            return username.lower()
    return None


def looks_like_control_token(text: str, username: str | None) -> bool:
    lowered = text.lower()
    if username and lowered == username.lower():
        return True
    if lowered in IGNORED_TOKENS:
        return True
    if lowered.startswith("view") and "repl" in lowered:
        return True
    if RELATIVE_TIME_RE.match(lowered):
        return True
    if LIKES_RE.match(lowered):
        return True
    if text in {"•", "·", "â€¢", "Â·"}:
        return True
    return False


def extract_comment_text(tokens: list[str], username: str | None) -> str | None:
    cleaned: list[str] = []
    for token in tokens:
        token_text = normalize_text(token)
        if not token_text:
            continue
        if looks_like_control_token(token_text, username):
            continue
        cleaned.append(token_text)

    if not cleaned:
        return None

    merged = " ".join(cleaned).strip()
    return merged or None


def extract_comment_media_url(row) -> str | None:
    candidates: list[str] = []

    # Anchors can include gif providers or media redirect URLs.
    for value in row.xpath(".//a/@href | .//img/@src | .//video/@src | .//source/@src"):
        text = normalize_text(value)
        if text:
            candidates.append(text)

    # Include all attributes to catch escaped or non-standard media URLs.
    for value in row.xpath(".//@*"):
        text = normalize_text(value)
        if text:
            candidates.append(text)

    row_html = normalize_text(html.tostring(row, encoding="unicode")) or ""
    for match in ESCAPED_HTTP_URL_RE.findall(row_html):
        candidates.append(match)
    for match in HTTP_URL_RE.findall(row_html):
        candidates.append(match)

    preferred_keywords = ("giphy", "tenor", ".gif", "fbcdn.net/emg1/", "utld=giphy.com", "media.giphy.com")
    blocked_keywords = ("/p/", "/reel/", "/accounts/login/")

    def decode_candidate(value: str) -> str:
        decoded = html_unescape(value)
        replacements = {
            "\\/": "/",
            "\\u002F": "/",
            "\\u002f": "/",
            "\\x2F": "/",
            "\\x2f": "/",
            "\\u003A": ":",
            "\\u003a": ":",
            "\\x3A": ":",
            "\\x3a": ":",
            "\\u0026": "&",
            "\\u003D": "=",
            "\\u003d": "=",
            "\\x3D": "=",
            "\\x3d": "=",
        }
        for old, new in replacements.items():
            decoded = decoded.replace(old, new)
        return decoded.strip(" '\"")

    decoded_candidates = [decode_candidate(candidate) for candidate in candidates]

    def looks_like_profile_picture(value: str) -> bool:
        lower = value.lower()
        return (
            "profile_pic" in lower
            or "profile picture" in lower
            or "dst-jpg_s150x150" in lower
            or "/t51.2885-19/" in lower
            or "/t51.75761-19/" in lower
            or "/t51.82787-19/" in lower
        )

    for candidate in decoded_candidates:
        lower = candidate.lower()
        if any(blocked in lower for blocked in blocked_keywords):
            continue
        if looks_like_profile_picture(candidate):
            continue
        if any(keyword in lower for keyword in preferred_keywords):
            return candidate

    # Fallback to any non-profile media URL if we could not find a giphy-like URL.
    for candidate in decoded_candidates:
        lower = candidate.lower()
        if any(blocked in lower for blocked in blocked_keywords):
            continue
        if looks_like_profile_picture(candidate):
            continue
        if lower.startswith("http://") or lower.startswith("https://"):
            return candidate
    return None


def parse_comment_row(row, comment_id: str | None) -> dict[str, Any] | None:
    username = extract_username_from_row(row)
    if not username:
        return None

    tokens = [normalize_text(text) for text in row.xpath(".//text()")]
    tokens = [token for token in tokens if token]
    comment_text = extract_comment_text(tokens, username)
    if not comment_text:
        media_url = extract_comment_media_url(row)
        if media_url:
            comment_text = media_url
    if not comment_text:
        return None

    row_text_blob = normalize_text(" ".join(tokens)) or ""
    likes_match = LIKES_RE.search(row_text_blob)
    likes_text = normalize_text(likes_match.group(1)) if likes_match else None
    likes = parse_compact_number(likes_text)

    reply_match = REPLY_COUNT_RE.search(row_text_blob)
    reply_count = int(reply_match.group(1)) if reply_match else None

    timestamp_raw = normalize_text(row.xpath("string(.//time/@datetime)"))
    published_at = to_iso_datetime(timestamp_raw)
    profile_picture = normalize_text(
        row.xpath(
            "string(.//img[contains(translate(@alt, 'PROFILE PICTURE', 'profile picture'), 'profile picture')][1]/@src)"
        )
    ) or normalize_text(row.xpath("string(.//img[1]/@src)"))

    mentions = [mention.lower() for mention in MENTION_RE.findall(comment_text)]
    return {
        "comment_id": comment_id,
        "username": username,
        "profile_picture": profile_picture,
        "text": comment_text,
        "published_at": published_at,
        "likes_text": likes_text,
        "likes": likes,
        "reply_count": reply_count,
        "mentions": sorted(set(mentions)),
        "replies": [],
        "text_length": len(comment_text),
    }


def extract_comments(root) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    seen_rows: set[int] = set()
    seen_keys: set[str] = set()

    def comment_key(comment: dict[str, Any]) -> str:
        comment_id = comment.get("comment_id")
        if comment_id:
            return f"id:{comment_id}"
        username = (comment.get("username") or "").lower()
        text = (comment.get("text") or "").strip().lower()
        published = (comment.get("published_at") or "").strip().lower()
        return f"f:{username}|{text}|{published}"

    permalink_anchors = root.xpath("//a[@href and contains(@href, '/p/') and contains(@href, '/c/')]")
    for anchor in permalink_anchors:
        href = normalize_text(anchor.get("href"))
        if not href:
            continue
        id_match = COMMENT_LINK_RE.match(href)
        if not id_match:
            continue
        comment_id = id_match.group(1)

        row = select_comment_row_container(anchor)
        if row is None:
            continue

        row_id = id(row)
        if row_id in seen_rows:
            continue
        seen_rows.add(row_id)

        parsed = parse_comment_row(row, comment_id)
        if parsed is None:
            continue

        key = comment_key(parsed)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        comments.append(parsed)

    # Fallback rows capture media-only comments without a visible /c/<id>/ permalink.
    fallback_media_nodes = root.xpath(
        "//img[contains(@src, 'fbcdn.net/emg1/') or contains(@src, 'giphy') or contains(@src, 'tenor') "
        "or contains(@src, '.gif') or contains(@src, 'utld=giphy.com')] "
        "| //video[contains(@src, 'giphy') or contains(@src, 'tenor') or contains(@src, '.gif')] "
        "| //a[contains(@href, 'giphy') or contains(@href, 'tenor') or contains(@href, '.gif')]"
    )
    for media_node in fallback_media_nodes:
        row = select_media_comment_row(media_node)
        if row is None:
            continue

        row_id = id(row)
        if row_id in seen_rows:
            continue

        parsed = parse_comment_row(row, None)
        if parsed is None:
            continue

        key = comment_key(parsed)
        if key in seen_keys:
            continue

        seen_rows.add(row_id)
        seen_keys.add(key)
        comments.append(parsed)

    return comments


def select_comment_row_container(anchor):
    candidates = anchor.xpath("ancestor::div[count(.//a[contains(@href, '/p/') and contains(@href, '/c/')])=1]")
    if not candidates:
        return None

    for candidate in candidates:
        tokens = [normalize_text(text) for text in candidate.xpath(".//text()")]
        tokens = [token for token in tokens if token]
        if not tokens:
            continue
        blob = " ".join(tokens).lower()
        if "reply" in blob and (" like " in f" {blob} " or " likes " in f" {blob} "):
            return candidate

    return candidates[0]


def select_media_comment_row(media_node):
    candidates = media_node.xpath("ancestor::div")
    for candidate in candidates:
        usernames: set[str] = set()
        for anchor in candidate.xpath(".//a[@href and contains(@class, 'notranslate')]"):
            href = normalize_text(anchor.get("href"))
            if not href:
                continue
            username_match = USERNAME_PATH_RE.match(href)
            if username_match:
                usernames.add(username_match.group(1).lower())

        # Media comment rows should map to exactly one commenter.
        if len(usernames) != 1:
            continue

        tokens = [normalize_text(text) for text in candidate.xpath(".//text()")]
        tokens = [token for token in tokens if token]
        if not tokens or len(tokens) > 80:
            continue

        blob = " ".join(tokens).lower()
        if "like" not in blob and "reply" not in blob:
            continue
        return candidate
    return None


def build_commenter_insights(comments: list[dict[str, Any]]) -> dict[str, Any]:
    commenter_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "comments_count": 0,
            "total_likes_on_comments": 0,
            "liked_comments_count": 0,
            "total_comment_length": 0,
            "first_comment_at": None,
            "last_comment_at": None,
            "mentions_made": Counter(),
        }
    )

    mention_counter: Counter[str] = Counter()
    comments_with_likes = 0
    total_comment_length = 0

    for comment in comments:
        username = comment["username"]
        likes = comment.get("likes")
        published_at = comment.get("published_at")
        mentions = comment.get("mentions") or []

        stat = commenter_stats[username]
        stat["comments_count"] += 1
        stat["total_comment_length"] += comment.get("text_length", 0)

        if likes is not None:
            stat["total_likes_on_comments"] += likes
            stat["liked_comments_count"] += 1
            comments_with_likes += 1

        if published_at:
            if stat["first_comment_at"] is None or published_at < stat["first_comment_at"]:
                stat["first_comment_at"] = published_at
            if stat["last_comment_at"] is None or published_at > stat["last_comment_at"]:
                stat["last_comment_at"] = published_at

        for mentioned_user in mentions:
            stat["mentions_made"][mentioned_user] += 1
            mention_counter[mentioned_user] += 1

        total_comment_length += comment.get("text_length", 0)

    commenters: dict[str, dict[str, Any]] = {}
    for username, stat in commenter_stats.items():
        comments_count = stat["comments_count"]
        liked_comments_count = stat["liked_comments_count"]
        commenters[username] = {
            "comments_count": comments_count,
            "total_likes_on_comments": stat["total_likes_on_comments"],
            "avg_likes_on_comments": round(stat["total_likes_on_comments"] / liked_comments_count, 2)
            if liked_comments_count
            else None,
            "avg_comment_length": round(stat["total_comment_length"] / comments_count, 2) if comments_count else 0,
            "first_comment_at": stat["first_comment_at"],
            "last_comment_at": stat["last_comment_at"],
            "mentions_made": dict(stat["mentions_made"].most_common()),
        }

    top_commenters_by_count = [
        {
            "username": username,
            "comments_count": stats["comments_count"],
            "total_likes_on_comments": stats["total_likes_on_comments"],
        }
        for username, stats in sorted(
            commenters.items(),
            key=lambda item: (item[1]["comments_count"], item[1]["total_likes_on_comments"]),
            reverse=True,
        )[:10]
    ]

    top_commenters_by_likes = [
        {
            "username": username,
            "total_likes_on_comments": stats["total_likes_on_comments"],
            "comments_count": stats["comments_count"],
        }
        for username, stats in sorted(
            commenters.items(),
            key=lambda item: (item[1]["total_likes_on_comments"], item[1]["comments_count"]),
            reverse=True,
        )[:10]
    ]

    top_comments_by_likes = [
        {
            "comment_id": comment["comment_id"],
            "username": comment["username"],
            "likes": comment["likes"],
            "text": comment["text"],
            "published_at": comment["published_at"],
        }
        for comment in sorted(
            comments,
            key=lambda item: (item.get("likes") or 0, item.get("text_length") or 0),
            reverse=True,
        )[:10]
    ]

    summary = {
        "total_visible_comments": len(comments),
        "unique_commenters": len(commenters),
        "comments_with_like_count_visible": comments_with_likes,
        "comments_with_mentions": sum(1 for comment in comments if comment.get("mentions")),
        "average_comment_length": round(total_comment_length / len(comments), 2) if comments else 0,
        "total_mentions": sum(mention_counter.values()),
    }

    return {
        "summary": summary,
        "top_commenters_by_count": top_commenters_by_count,
        "top_commenters_by_likes_received": top_commenters_by_likes,
        "top_mentions": [{"username": username, "count": count} for username, count in mention_counter.most_common(20)],
        "top_comments_by_likes": top_comments_by_likes,
        "commenters": commenters,
    }


def to_simple_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": comment.get("username"),
        "profile_picture": comment.get("profile_picture"),
        "likes": comment.get("likes"),
        "text": comment.get("text"),
        "replies": comment.get("replies") or [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse Instagram saved HTML and build commenter insights.")
    parser.add_argument("--html", default=str(DEFAULT_HTML_FILE), help="Path to saved Instagram HTML file.")
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT_FILE), help="Path to output JSON file.")
    return parser.parse_args()


def build_result_from_root(root, source_html: str | None = None) -> dict[str, Any]:
    post_meta = parse_post_meta(root)
    comments = extract_comments(root)
    insights = build_commenter_insights(comments)
    simple_comments = [to_simple_comment(comment) for comment in comments]

    return {
        "meta": {
            "source_html": source_html,
            "generated_at": utc_now_iso(),
        },
        "post": post_meta,
        "comments_extracted": len(comments),
        "comments": simple_comments,
        "insights": insights,
    }


def parse_html_text(html_text: str, source_html: str | None = None) -> dict[str, Any]:
    root = html.fromstring(html_text)
    return build_result_from_root(root, source_html=source_html)


def parse_html_file(path: str | Path) -> dict[str, Any]:
    html_path = Path(path)
    if not html_path.exists():
        raise FileNotFoundError(f"Input HTML file not found: {html_path}")

    html_text = html_path.read_text(encoding="utf-8", errors="ignore")
    return parse_html_text(html_text, source_html=str(html_path))


def main() -> None:
    args = parse_args()
    html_path = Path(args.html)
    output_path = Path(args.out)

    result = parse_html_file(html_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    comments_count = len(result.get("comments") or [])
    insights = result.get("insights") or {}
    summary = insights.get("summary") or {}
    top_commenters = insights.get("top_commenters_by_count") or []

    print(f"Saved insights: {output_path.resolve()}")
    print(f"Visible comments extracted: {comments_count}")
    print(f"Unique commenters: {summary.get('unique_commenters')}")
    print("Top commenters by count:")
    for item in top_commenters[:5]:
        print(
            f"  - {item['username']}: comments={item['comments_count']}, likes_on_comments={item['total_likes_on_comments']}"
        )


if __name__ == "__main__":
    main()
