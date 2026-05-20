import re
from bs4 import BeautifulSoup
from urllib.parse import urlsplit

PROFILE_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?$",
    re.IGNORECASE,
)

COUNT_RE = re.compile(
    r"(?P<followers>[\d.,]+(?:[KMB])?)\s*Followers?\s*,\s*"
    r"(?P<following>[\d.,]+(?:[KMB])?)\s*Following\b",
    re.IGNORECASE,
)

BLOCKED_FIRST_PATHS = {
    "p", "reel", "reels", "stories", "explore", "accounts", "tv", "about"
}


def is_real_instagram_profile_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
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

    if parts[0].lower() in BLOCKED_FIRST_PATHS:
        return False

    return True


def normalize_profile_url(url: str) -> str:
    m = PROFILE_URL_RE.match(url)
    if not m:
        return ""
    username = m.group(1)
    return f"https://www.instagram.com/{username}/"


def extract_instagram_profiles_with_counts_from_ddg_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    for article in soup.select('article[data-testid="result"]'):
        title_link = article.select_one('a[data-testid="result-title-a"]')
        if not title_link:
            continue

        href = (title_link.get("href") or "").strip()
        if not is_real_instagram_profile_url(href):
            continue

        profile_url = normalize_profile_url(href)
        if not profile_url:
            continue

        username = profile_url.rstrip("/").split("/")[-1]

        snippet_el = article.select_one('div[data-result="snippet"]')
        snippet_text = snippet_el.get_text(" ", strip=True) if snippet_el else ""

        followers = None
        following = None

        m = COUNT_RE.search(snippet_text)
        if m:
            followers = m.group("followers").replace(" ", "")
            following = m.group("following").replace(" ", "")

        row = {
            "username": username,
            "profile_url": profile_url,
            "followers": followers,
            "following": following,
            "snippet": snippet_text,
        }

        key = profile_url
        if key not in seen:
            seen.add(key)
            results.append(row)

    return results