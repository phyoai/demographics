from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import quote

import scrapy
from scrapy.http import Request, Response

try:
    from scrapy_playwright.page import PageMethod
except Exception:  # pragma: no cover
    PageMethod = None

from ..items import InstagramPostItem, InstagramProfileItem
from ..utils import (
    canonicalize_instagram_url,
    deep_find_values,
    extract_json_objects_from_scripts,
    extract_shortcode_from_url,
    get_nested_value,
    iter_dicts,
    normalize_text,
    parse_compact_number,
    safe_json_loads,
    to_iso_datetime,
    unique_preserve_order,
    utc_now_iso,
)


class InstagramProfileSpider(scrapy.Spider):
    name = "instagram_profile"
    allowed_domains = ["instagram.com", "www.instagram.com"]

    PROFILE_FOLLOWERS_RE = re.compile(r"([\d.,]+(?:\s*[KMBT])?)\s+Followers?", re.IGNORECASE)
    PROFILE_FOLLOWING_RE = re.compile(r"([\d.,]+(?:\s*[KMBT])?)\s+Following", re.IGNORECASE)
    PROFILE_POSTS_RE = re.compile(r"([\d.,]+(?:\s*[KMBT])?)\s+Posts?", re.IGNORECASE)

    METRIC_LIKES_RE = re.compile(r"([\d.,]+(?:\s*[KMBT])?)\s+likes?", re.IGNORECASE)
    METRIC_COMMENTS_RE = re.compile(r"([\d.,]+(?:\s*[KMBT])?)\s+comments?", re.IGNORECASE)
    METRIC_VIEWS_RE = re.compile(r"([\d.,]+(?:\s*[KMBT])?)\s+views?", re.IGNORECASE)

    USERNAME_HANDLE_RE = re.compile(r"\(@([A-Za-z0-9._]+)\)")
    USERNAME_PATH_RE = re.compile(r"^/([A-Za-z0-9._]+)/$")
    URL_DISCOVERY_RE = re.compile(r"/(p|reel)/([A-Za-z0-9_-]{8,20})")
    URL_DISCOVERY_ESCAPED_RE = re.compile(r"\\u002F(p|reel)\\u002F([A-Za-z0-9_-]{8,20})")
    SHORTCODE_JSON_RE = re.compile(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{8,20})"')
    SHORTCODE_ESCAPED_JSON_RE = re.compile(r'\\"shortcode\\"\s*:\s*\\"([A-Za-z0-9_-]{8,20})\\"')
    CODE_JSON_RE = re.compile(r'"code"\s*:\s*"([A-Za-z0-9_-]{8,20})"')
    CODE_ESCAPED_JSON_RE = re.compile(r'\\"code\\"\s*:\s*\\"([A-Za-z0-9_-]{8,20})\\"')
    SHORTCODE_LIKE_RE = re.compile(r"^[A-Za-z0-9_-]{8,20}$")
    LOCALE_CODE_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}$")

    def __init__(self, username: str | None = None, usernames: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_usernames = self._parse_usernames(username, usernames)
        if not self.target_usernames:
            raise ValueError("Provide -a username=<name> or -a usernames=<name1,name2>.")

        self.use_playwright_fallback = False
        self.playwright_available = False
        self.playwright_handler_enabled = False
        self.scrape_comments = True
        self.max_visible_comments = 50
        self.max_recent_posts = 20
        self.max_recent_reels = 20
        self.request_timeout = 30
        self._scheduled_post_urls: set[str] = set()
        self._prefetched_nodes: dict[tuple[str, str], dict[str, Any]] = {}

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)

        spider.max_visible_comments = max(0, crawler.settings.getint("MAX_VISIBLE_COMMENTS", 50))
        spider.max_recent_posts = max(0, crawler.settings.getint("MAX_RECENT_POSTS", 20))
        spider.max_recent_reels = max(0, crawler.settings.getint("MAX_RECENT_REELS", 20))
        spider.request_timeout = crawler.settings.getint("DOWNLOAD_TIMEOUT", 30)
        spider.playwright_available = crawler.settings.getbool("PLAYWRIGHT_AVAILABLE", False)
        spider.playwright_handler_enabled = crawler.settings.getbool("PLAYWRIGHT_HANDLER_ENABLED", False)
        spider.scrape_comments = crawler.settings.getbool("SCRAPE_COMMENTS", True)

        use_playwright_arg = kwargs.get("use_playwright")
        if use_playwright_arg is None:
            spider.use_playwright_fallback = crawler.settings.getbool("USE_PLAYWRIGHT_FALLBACK", False)
        else:
            spider.use_playwright_fallback = spider._parse_bool(use_playwright_arg)

        scrape_comments_arg = kwargs.get("scrape_comments")
        if scrape_comments_arg is not None:
            spider.scrape_comments = spider._parse_bool(scrape_comments_arg)

        if spider.use_playwright_fallback and not spider.playwright_available:
            spider.logger.warning(
                "Playwright fallback requested but scrapy-playwright is unavailable. "
                "Run pip install -r requirements.txt and playwright install chromium."
            )
            spider.use_playwright_fallback = False
        elif spider.use_playwright_fallback and not spider.playwright_handler_enabled:
            spider.logger.warning(
                "Playwright fallback requested but handler is disabled. "
                "Run with `-s USE_PLAYWRIGHT_FALLBACK=1` or set env `USE_PLAYWRIGHT_FALLBACK=1`."
            )
            spider.use_playwright_fallback = False
        return spider

    async def start(self):
        for username in self.target_usernames:
            profile_url = f"https://www.instagram.com/{username}/"
            self.logger.info("Username started: %s", username)
            yield self._build_request(
                url=profile_url,
                callback=self.parse_profile,
                username=username,
                request_kind="profile",
            )

    def parse_profile(self, response: Response):
        username = response.meta.get("username")
        if not username:
            return

        if response.status == 404:
            self.logger.warning("Profile not found or inaccessible: %s", username)
            yield InstagramProfileItem(**self._build_empty_profile_item(username, response.url))
            return

        if response.status >= 400:
            self.logger.warning("Unexpected status %s for profile %s", response.status, response.url)

        try:
            profile_item_data = self._extract_profile_data(response, username)
        except Exception:
            self.logger.exception("Failed to parse profile for username=%s", username)
            profile_item_data = self._build_empty_profile_item(username, response.url)

        needs_api_fallback = self._needs_profile_api_fallback(profile_item_data)
        if self._needs_profile_playwright_retry(profile_item_data) and self._can_retry_with_playwright(response.request):
            self.logger.info("Profile parse weak; retrying with Playwright fallback for username=%s", username)
            yield self._clone_with_playwright(response.request)
            return

        yield InstagramProfileItem(**profile_item_data)
        self.logger.info(
            "Profile parsed: %s (posts=%s reels=%s)",
            username,
            len(profile_item_data.get("recent_posts") or []),
            len(profile_item_data.get("recent_reels") or []),
        )

        discovered_urls = unique_preserve_order(
            (profile_item_data.get("recent_posts") or []) + (profile_item_data.get("recent_reels") or [])
        )
        for request in self._schedule_post_requests(username, discovered_urls):
            yield request

        if needs_api_fallback:
            self.logger.info("Profile HTML is sparse; requesting public profile JSON fallback for username=%s", username)
            yield self._build_public_profile_api_request(username)

    def parse_profile_api(self, response: Response):
        username = response.meta.get("username")
        if not username:
            return

        if response.status >= 400:
            self.logger.warning("Public profile JSON fallback failed for %s with status=%s", username, response.status)
            return

        payload = self._safe_instagram_json_payload(response.text)
        if not isinstance(payload, dict):
            self.logger.warning("Public profile JSON fallback returned invalid JSON for username=%s", username)
            return

        profile_item_data, prefetched_nodes = self._extract_profile_data_from_public_api(payload, username)
        if profile_item_data is None:
            self.logger.warning("Public profile JSON fallback has no usable user data for username=%s", username)
            return

        yield InstagramProfileItem(**profile_item_data)
        self.logger.info(
            "Profile parsed from public JSON fallback: %s (posts=%s reels=%s)",
            username,
            len(profile_item_data.get("recent_posts") or []),
            len(profile_item_data.get("recent_reels") or []),
        )

        post_urls = unique_preserve_order(
            (profile_item_data.get("recent_posts") or []) + (profile_item_data.get("recent_reels") or [])
        )
        for request in self._schedule_post_requests(
            username=username,
            urls=post_urls,
            prefetched_nodes=prefetched_nodes,
            priority=20,
        ):
            yield request

    def _build_public_profile_api_request(self, username: str) -> Request:
        api_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={quote(username)}"
        headers = {
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/{username}/",
        }
        return self._build_request(
            url=api_url,
            callback=self.parse_profile_api,
            username=username,
            request_kind="profile_api",
            priority=5,
            dont_filter=True,
            headers=headers,
        )

    def _build_public_post_api_request(
        self,
        username: str,
        post_url: str,
        shortcode_hint: str | None = None,
    ) -> Request | None:
        canonical_post_url = canonicalize_instagram_url(post_url) or post_url
        shortcode = normalize_text(shortcode_hint) or extract_shortcode_from_url(canonical_post_url)
        if not self._is_valid_shortcode(shortcode):
            return None

        path_kind = "reel" if "/reel/" in canonical_post_url else "p"
        api_url = f"https://www.instagram.com/{path_kind}/{shortcode}/?__a=1&__d=dis"
        headers = {
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": canonical_post_url,
        }
        return self._build_request(
            url=api_url,
            callback=self.parse_post_api,
            username=username,
            request_kind="post_api",
            priority=15,
            dont_filter=True,
            headers=headers,
            extra_meta={
                "post_api_retry": True,
                "shortcode_hint": shortcode,
                "original_post_url": canonical_post_url,
            },
        )

    def _schedule_post_requests(
        self,
        username: str,
        urls: list[str],
        prefetched_nodes: dict[str, dict[str, Any]] | None = None,
        priority: int = 10,
    ):
        prefetched_nodes = prefetched_nodes or {}
        for post_url in unique_preserve_order(urls):
            canonical_url = canonicalize_instagram_url(post_url)
            if not canonical_url:
                continue
            shortcode = extract_shortcode_from_url(canonical_url)
            if ("/p/" in canonical_url or "/reel/" in canonical_url) and not self._is_valid_shortcode(shortcode):
                continue
            schedule_key = f"{username}|{canonical_url}"
            if schedule_key in self._scheduled_post_urls:
                continue
            self._scheduled_post_urls.add(schedule_key)

            if shortcode and shortcode in prefetched_nodes:
                self._prefetched_nodes[(username, shortcode)] = prefetched_nodes[shortcode]

            yield self._build_request(
                url=canonical_url,
                callback=self.parse_post,
                username=username,
                request_kind="post",
                priority=priority,
                extra_meta={"shortcode_hint": shortcode},
            )

    def parse_post(self, response: Response):
        username = response.meta.get("username")
        if not username:
            return

        if response.status == 404:
            self.logger.warning("Post or reel not found: %s", response.url)
            yield InstagramPostItem(**self._build_empty_post_item(response.url, username))
            return

        if response.status >= 400:
            self.logger.warning("Unexpected status %s for %s", response.status, response.url)

        try:
            post_item_data = self._extract_post_data(response, username)
        except Exception:
            self.logger.exception("Failed to parse post page: %s", response.url)
            post_item_data = self._build_empty_post_item(response.url, username)

        shortcode_hint = response.meta.get("shortcode_hint")
        if post_item_data.get("shortcode") is None and shortcode_hint and self._is_valid_shortcode(shortcode_hint):
            post_item_data["shortcode"] = shortcode_hint

        prefetched_entry = self._get_prefetched_post_entry(
            username=username,
            shortcode=post_item_data.get("shortcode"),
            post_url=post_item_data.get("post_url"),
        )
        if prefetched_entry and self._needs_prefetched_post_enrichment(post_item_data):
            post_item_data = self._enrich_post_item_from_prefetched(post_item_data, prefetched_entry)

        if self.scrape_comments and self._needs_comment_playwright_retry(post_item_data) and self._can_retry_with_playwright(response.request):
            self.logger.info("Comments empty; retrying with Playwright fallback for %s", response.url)
            yield self._clone_with_playwright(response.request)
            return

        if self._needs_post_playwright_retry(post_item_data) and self._can_retry_with_playwright(response.request):
            self.logger.info("Post parse weak; retrying with Playwright fallback for %s", response.url)
            yield self._clone_with_playwright(response.request)
            return

        if self.scrape_comments and not (post_item_data.get("comments") or []) and not response.meta.get("post_api_retry"):
            post_api_request = self._build_public_post_api_request(
                username=username,
                post_url=post_item_data.get("post_url") or response.url,
                shortcode_hint=post_item_data.get("shortcode") or response.meta.get("shortcode_hint"),
            )
            if post_api_request is not None:
                self.logger.info("Comments empty; requesting public post JSON fallback for %s", response.url)
                yield post_api_request

        if post_item_data.get("owner_username") is None:
            post_item_data["owner_username"] = username

        if not self._is_valid_shortcode(post_item_data.get("shortcode")):
            self.logger.info(
                "Discarding non-media post candidate url=%s shortcode=%s",
                post_item_data.get("post_url"),
                post_item_data.get("shortcode"),
            )
            return

        if self.scrape_comments:
            comment_count = len(post_item_data.get("comments") or [])
            if comment_count > 0:
                self.logger.info(
                    "Comment extraction success for shortcode=%s: %s visible comments",
                    post_item_data.get("shortcode"),
                    comment_count,
                )
            else:
                self.logger.info("No visible comments extracted for %s", response.url)
        else:
            post_item_data["comments"] = []

        self.logger.info("Post parsed: %s", post_item_data.get("post_url"))
        yield InstagramPostItem(**post_item_data)

    def parse_post_api(self, response: Response):
        username = response.meta.get("username")
        if not username:
            return

        if response.status >= 400:
            self.logger.warning("Public post JSON fallback failed with status=%s for %s", response.status, response.url)
            return

        payload = self._safe_instagram_json_payload(response.text)
        if not isinstance(payload, dict):
            self.logger.warning("Public post JSON fallback returned invalid JSON for %s", response.url)
            return

        original_post_url = normalize_text(response.meta.get("original_post_url")) or response.url.split("?", 1)[0]
        shortcode_hint = normalize_text(response.meta.get("shortcode_hint"))

        media_node = self._select_media_node([payload], shortcode_hint)
        if not isinstance(media_node, dict):
            media_node = self._first_non_null(
                [
                    get_nested_value(payload, "graphql.shortcode_media"),
                    get_nested_value(payload, "data.shortcode_media"),
                    get_nested_value(payload, "data.xdt_shortcode_media"),
                ]
            )
        if not isinstance(media_node, dict):
            self.logger.info("Public post JSON fallback has no usable media node for %s", original_post_url)
            return

        post_item_data = self._build_empty_post_item(original_post_url, username)
        if shortcode_hint and not post_item_data.get("shortcode"):
            post_item_data["shortcode"] = shortcode_hint

        prefetched_entry = {
            "node": media_node,
            "source": "post_json_fallback",
            "post_type": "reel" if "/reel/" in (post_item_data.get("post_url") or "") else "post",
        }
        post_item_data = self._enrich_post_item_from_prefetched(post_item_data, prefetched_entry)
        if post_item_data.get("owner_username") is None:
            post_item_data["owner_username"] = username
        post_item_data["scraped_at"] = utc_now_iso()

        if self.scrape_comments:
            comment_count = len(post_item_data.get("comments") or [])
            if comment_count > 0:
                self.logger.info(
                    "Comment extraction success from post JSON fallback for shortcode=%s: %s visible comments",
                    post_item_data.get("shortcode"),
                    comment_count,
                )
            else:
                self.logger.info(
                    "Public post JSON fallback found no visible comments for %s",
                    post_item_data.get("post_url"),
                )
        else:
            post_item_data["comments"] = []
        yield InstagramPostItem(**post_item_data)

    def _get_prefetched_post_entry(
        self,
        username: str,
        shortcode: str | None,
        post_url: str | None,
    ) -> dict[str, Any] | None:
        effective_shortcode = normalize_text(shortcode) or extract_shortcode_from_url(post_url)
        if not self._is_valid_shortcode(effective_shortcode):
            return None
        return self._prefetched_nodes.get((username, effective_shortcode))

    def _enrich_post_item_from_prefetched(
        self,
        post_item: dict[str, Any],
        prefetched_entry: dict[str, Any],
    ) -> dict[str, Any]:
        node = prefetched_entry.get("node")
        if not isinstance(node, dict):
            return post_item

        shortcode = normalize_text(node.get("shortcode"))
        if shortcode:
            post_item["shortcode"] = shortcode

        post_item["post_type"] = self._first_non_null(
            [
                normalize_text(prefetched_entry.get("post_type")),
                "reel" if normalize_text(node.get("product_type")) == "clips" else None,
                "post" if normalize_text(node.get("shortcode")) else None,
                post_item.get("post_type"),
            ]
        )
        post_item["caption"] = self._first_non_null([post_item.get("caption"), self._extract_caption_from_media(node)])
        post_item["published_at"] = self._first_non_null(
            [post_item.get("published_at"), to_iso_datetime(node.get("taken_at_timestamp"))]
        )

        likes = self._as_int(
            self._first_non_null(
                [
                    get_nested_value(node, "edge_media_preview_like.count"),
                    get_nested_value(node, "edge_liked_by.count"),
                    node.get("like_count"),
                ]
            )
        )
        comments_count = self._as_int(
            self._first_non_null(
                [
                    get_nested_value(node, "edge_media_to_parent_comment.count"),
                    get_nested_value(node, "edge_media_to_comment.count"),
                    node.get("comment_count"),
                ]
            )
        )
        views = self._as_int(
            self._first_non_null(
                [
                    node.get("video_view_count"),
                    node.get("video_play_count"),
                    node.get("view_count"),
                ]
            )
        )

        post_item["likes"] = self._first_non_null([post_item.get("likes"), likes])
        post_item["comments_count"] = self._first_non_null([post_item.get("comments_count"), comments_count])
        post_item["views"] = self._first_non_null([post_item.get("views"), views])
        post_item["likes_text"] = self._first_non_null(
            [post_item.get("likes_text"), str(post_item["likes"]) if post_item.get("likes") is not None else None]
        )
        post_item["comments_count_text"] = self._first_non_null(
            [
                post_item.get("comments_count_text"),
                str(post_item["comments_count"]) if post_item.get("comments_count") is not None else None,
            ]
        )
        post_item["views_text"] = self._first_non_null(
            [post_item.get("views_text"), str(post_item["views"]) if post_item.get("views") is not None else None]
        )
        post_item["owner_username"] = self._first_non_null(
            [post_item.get("owner_username"), normalize_text(get_nested_value(node, "owner.username"))]
        )

        merged_media_urls = unique_preserve_order(
            (post_item.get("thumbnails_or_media_urls") or []) + self._extract_media_urls(node)
        )
        post_item["thumbnails_or_media_urls"] = merged_media_urls

        if self.scrape_comments and not post_item.get("comments"):
            comments = self._extract_comments_from_media_node(node, post_item.get("owner_username"))
            post_item["comments"] = self._dedupe_comments(comments)[: self.max_visible_comments]

        return post_item

    def handle_request_error(self, failure):
        request = failure.request
        username = request.meta.get("username")
        request_kind = request.meta.get("request_kind")
        self.logger.warning(
            "Request failed for username=%s url=%s reason=%r",
            username,
            request.url,
            failure.value,
        )

        if request_kind == "profile" and username:
            self.logger.info("Profile request failed; trying public profile JSON fallback for username=%s", username)
            return self._build_public_profile_api_request(username)

        if self._can_retry_with_playwright(request):
            self.logger.info("Retrying with Playwright fallback: %s", request.url)
            return self._clone_with_playwright(request)
        return None

    def _build_request(
        self,
        url: str,
        callback,
        username: str,
        request_kind: str,
        priority: int = 0,
        dont_filter: bool = False,
        playwright: bool = False,
        headers: dict[str, str] | None = None,
        extra_meta: dict[str, Any] | None = None,
    ) -> Request:
        meta = {
            "username": username,
            "request_kind": request_kind,
            "download_timeout": self.request_timeout,
            "handle_httpstatus_all": True,
        }
        if extra_meta:
            meta.update(extra_meta)
        if playwright:
            meta.update(self._playwright_request_meta(request_kind))
            meta["playwright_retry"] = True

        return Request(
            url=url,
            callback=callback,
            errback=self.handle_request_error,
            priority=priority,
            dont_filter=dont_filter,
            meta=meta,
            headers=headers,
        )

    def _playwright_request_meta(self, request_kind: str | None = None) -> dict[str, Any]:
        meta = {
            "playwright": True,
            "playwright_include_page": False,
            "playwright_page_goto_kwargs": {"wait_until": "domcontentloaded"},
        }
        if PageMethod is not None:
            if request_kind == "post":
                meta["playwright_page_methods"] = self._playwright_post_page_methods()
            elif request_kind == "profile":
                meta["playwright_page_methods"] = self._playwright_profile_page_methods()
        return meta

    def _playwright_profile_page_methods(self) -> list[Any]:
        # Instagram profile grids often hydrate after domcontentloaded; give it a short settle+scroll.
        return [
            PageMethod("wait_for_timeout", 1800),
            PageMethod("evaluate", self._playwright_profile_scroll_script()),
            PageMethod("wait_for_timeout", 1200),
            PageMethod("evaluate", self._playwright_collect_profile_links_script()),
            PageMethod("evaluate", self._playwright_profile_scroll_script()),
            PageMethod("wait_for_timeout", 1200),
            PageMethod("evaluate", self._playwright_collect_profile_links_script()),
        ]

    def _playwright_profile_scroll_script(self) -> str:
        return """
() => {
  const maxY = Math.max(
    document.documentElement?.scrollHeight || 0,
    document.body?.scrollHeight || 0
  );
  window.scrollTo(0, Math.min(maxY, Math.floor(maxY * 0.75)));
  return maxY;
}
"""

    def _playwright_collect_profile_links_script(self) -> str:
        return """
() => {
  const urls = [];
  const add = (href) => {
    if (!href || typeof href !== "string") return;
    if (!href.includes("/p/") && !href.includes("/reel/")) return;
    urls.push(href);
  };
  for (const a of Array.from(document.querySelectorAll("a[href*='/p/'], a[href*='/reel/']"))) {
    add(a.getAttribute("href"));
    add(a.href);
  }
  return urls;
}
"""

    def _playwright_post_page_methods(self) -> list[Any]:
        if not self.scrape_comments:
            return []
        # Try enough cycles to approach MAX_VISIBLE_COMMENTS without overloading requests.
        cycles = max(3, min(14, (self.max_visible_comments + 4) // 5))
        methods: list[Any] = [PageMethod("wait_for_timeout", 1800)]
        for _ in range(cycles):
            methods.append(PageMethod("evaluate", self._playwright_expand_comment_controls_script()))
            methods.append(PageMethod("wait_for_timeout", 450))
            methods.append(PageMethod("evaluate", self._playwright_scroll_for_comments_script()))
            methods.append(PageMethod("wait_for_timeout", 850))
        methods.append(PageMethod("wait_for_timeout", 1200))
        return methods

    def _playwright_expand_comment_controls_script(self) -> str:
        return """
() => {
  const expandKeywords = [
    "view all comments",
    "view more comments",
    "load more comments",
    "more comments",
    "view all",
    "view replies",
    "view more replies",
    "more replies",
    "show more"
  ];
  const isVisible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  let clicks = 0;
  const nodes = Array.from(document.querySelectorAll("button, a, span, div[role='button']"));
  for (const node of nodes) {
    const text = (node.innerText || node.textContent || "").trim().toLowerCase();
    if (!text) continue;
    if (!expandKeywords.some((kw) => text.includes(kw))) continue;
    if (!isVisible(node)) continue;
    try {
      node.click();
      clicks += 1;
    } catch (e) {
      // ignore click failures
    }
  }
  return clicks;
}
"""

    def _playwright_scroll_for_comments_script(self) -> str:
        return """
() => {
  const isScrollable = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const overflowY = style.overflowY;
    return (overflowY === "auto" || overflowY === "scroll") && el.scrollHeight > el.clientHeight + 40;
  };

  let scrolled = 0;
  const candidates = Array.from(document.querySelectorAll("main, section, article, div"));
  for (const el of candidates) {
    if (!isScrollable(el)) continue;
    el.scrollTop = el.scrollHeight;
    scrolled += 1;
  }
  window.scrollTo(0, document.body.scrollHeight);
  return scrolled;
}
"""

    def _can_retry_with_playwright(self, request: Request) -> bool:
        if not self.playwright_available:
            return False
        if not self.playwright_handler_enabled:
            return False
        if not self.use_playwright_fallback:
            return False
        if request.meta.get("playwright"):
            return False
        if request.meta.get("playwright_retry"):
            return False
        return True

    def _clone_with_playwright(self, request: Request) -> Request:
        cloned_meta = dict(request.meta)
        cloned_meta.update(self._playwright_request_meta(request.meta.get("request_kind")))
        cloned_meta["playwright_retry"] = True
        cloned_meta["download_timeout"] = self.request_timeout
        return request.replace(meta=cloned_meta, dont_filter=True, priority=request.priority + 1)

    def _extract_profile_data(self, response: Response, username: str) -> dict[str, Any]:
        profile_item = self._build_empty_profile_item(username, response.url)
        json_ld_objects, script_objects = self._extract_page_json(response)

        profile_candidate = self._select_profile_candidate(script_objects, username)
        json_ld_person = self._select_json_ld_person(json_ld_objects, username)

        meta_description = self._first_non_null(
            [
                normalize_text(response.xpath("//meta[@property='og:description']/@content").get()),
                normalize_text(response.xpath("//meta[@name='description']/@content").get()),
            ]
        )
        count_data = self._extract_profile_counts(meta_description or "")

        followers = self._as_int(
            self._first_non_null(
                [
                    get_nested_value(profile_candidate, "edge_followed_by.count"),
                    profile_candidate.get("follower_count") if isinstance(profile_candidate, dict) else None,
                    profile_candidate.get("followers") if isinstance(profile_candidate, dict) else None,
                    count_data.get("followers"),
                    self._first_non_null(deep_find_values(script_objects, "follower_count")),
                ]
            )
        )
        following = self._as_int(
            self._first_non_null(
                [
                    get_nested_value(profile_candidate, "edge_follow.count"),
                    profile_candidate.get("following_count") if isinstance(profile_candidate, dict) else None,
                    profile_candidate.get("following") if isinstance(profile_candidate, dict) else None,
                    count_data.get("following"),
                ]
            )
        )
        posts = self._as_int(
            self._first_non_null(
                [
                    get_nested_value(profile_candidate, "edge_owner_to_timeline_media.count"),
                    profile_candidate.get("media_count") if isinstance(profile_candidate, dict) else None,
                    profile_candidate.get("posts") if isinstance(profile_candidate, dict) else None,
                    count_data.get("posts"),
                ]
            )
        )

        profile_item["full_name"] = self._first_non_null(
            [
                normalize_text(profile_candidate.get("full_name")) if isinstance(profile_candidate, dict) else None,
                normalize_text(json_ld_person.get("name")) if isinstance(json_ld_person, dict) else None,
                self._extract_full_name_from_title(response.xpath("//title/text()").get()),
            ]
        )
        profile_item["bio"] = self._first_non_null(
            [
                normalize_text(profile_candidate.get("biography")) if isinstance(profile_candidate, dict) else None,
                normalize_text(json_ld_person.get("description")) if isinstance(json_ld_person, dict) else None,
            ]
        )
        profile_item["external_url"] = self._first_non_null(
            [
                normalize_text(profile_candidate.get("external_url")) if isinstance(profile_candidate, dict) else None,
                normalize_text(profile_candidate.get("external_url_linkshimmed"))
                if isinstance(profile_candidate, dict)
                else None,
                self._extract_json_ld_external_url(json_ld_person),
            ]
        )
        profile_item["category"] = self._first_non_null(
            [
                normalize_text(profile_candidate.get("category_name")) if isinstance(profile_candidate, dict) else None,
                normalize_text(profile_candidate.get("business_category_name"))
                if isinstance(profile_candidate, dict)
                else None,
            ]
        )

        is_verified_candidate = self._first_non_null(
            [
                profile_candidate.get("is_verified") if isinstance(profile_candidate, dict) else None,
                profile_candidate.get("verified") if isinstance(profile_candidate, dict) else None,
            ]
        )
        profile_item["is_verified"] = is_verified_candidate if isinstance(is_verified_candidate, bool) else None

        profile_item["followers"] = followers
        profile_item["following"] = following
        profile_item["posts"] = posts
        profile_item["followers_text"] = self._first_non_null(
            [count_data.get("followers_text"), str(followers) if followers is not None else None]
        )
        profile_item["following_text"] = self._first_non_null(
            [count_data.get("following_text"), str(following) if following is not None else None]
        )
        profile_item["posts_text"] = self._first_non_null(
            [count_data.get("posts_text"), str(posts) if posts is not None else None]
        )

        profile_item["profile_image_url"] = self._first_non_null(
            [
                normalize_text(profile_candidate.get("profile_pic_url_hd")) if isinstance(profile_candidate, dict) else None,
                normalize_text(profile_candidate.get("profile_pic_url")) if isinstance(profile_candidate, dict) else None,
                normalize_text(self._extract_json_ld_image_url(json_ld_person)),
                normalize_text(response.xpath("//meta[@property='og:image']/@content").get()),
            ]
        )

        recent_posts, recent_reels = self._extract_recent_urls(response, script_objects=script_objects)
        profile_item["recent_posts"] = self._augment_recent_posts_with_reel_equivalents(recent_posts, recent_reels)
        profile_item["recent_reels"] = recent_reels
        profile_item["scraped_at"] = utc_now_iso()
        return profile_item

    def _extract_profile_data_from_public_api(
        self, payload: dict[str, Any], username: str
    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
        user = get_nested_value(payload, "data.user")
        if not isinstance(user, dict):
            return None, {}

        profile_item = self._build_empty_profile_item(username, f"https://www.instagram.com/{username}/")
        followers = self._as_int(get_nested_value(user, "edge_followed_by.count"))
        following = self._as_int(get_nested_value(user, "edge_follow.count"))
        posts = self._as_int(get_nested_value(user, "edge_owner_to_timeline_media.count"))

        recent_posts, recent_reels, prefetched_nodes = self._extract_recent_urls_from_public_api_user(user)

        profile_item["full_name"] = self._first_non_null([normalize_text(user.get("full_name")), profile_item["full_name"]])
        profile_item["bio"] = self._first_non_null([normalize_text(user.get("biography")), profile_item["bio"]])
        profile_item["external_url"] = self._extract_external_url_from_public_api_user(user)
        profile_item["category"] = self._first_non_null(
            [normalize_text(user.get("category_name")), normalize_text(user.get("business_category_name"))]
        )
        is_verified_raw = user.get("is_verified")
        profile_item["is_verified"] = is_verified_raw if isinstance(is_verified_raw, bool) else None

        profile_item["followers"] = followers
        profile_item["following"] = following
        profile_item["posts"] = posts
        profile_item["followers_text"] = str(followers) if followers is not None else None
        profile_item["following_text"] = str(following) if following is not None else None
        profile_item["posts_text"] = str(posts) if posts is not None else None

        profile_item["profile_image_url"] = self._first_non_null(
            [normalize_text(user.get("profile_pic_url_hd")), normalize_text(user.get("profile_pic_url"))]
        )
        profile_item["recent_posts"] = self._augment_recent_posts_with_reel_equivalents(recent_posts, recent_reels)
        profile_item["recent_reels"] = recent_reels
        profile_item["scraped_at"] = utc_now_iso()
        return profile_item, prefetched_nodes

    def _extract_external_url_from_public_api_user(self, user: dict[str, Any]) -> str | None:
        direct = normalize_text(user.get("external_url"))
        if direct:
            return direct

        bio_links = user.get("bio_links")
        if isinstance(bio_links, list):
            for entry in bio_links:
                if not isinstance(entry, dict):
                    continue
                url = normalize_text(entry.get("url"))
                if url:
                    return url
        return None

    def _extract_recent_urls_from_public_api_user(
        self, user: dict[str, Any]
    ) -> tuple[list[str], list[str], dict[str, dict[str, Any]]]:
        recent_posts: list[str] = []
        recent_reels: list[str] = []
        prefetched_nodes: dict[str, dict[str, Any]] = {}

        timeline_edges = get_nested_value(user, "edge_owner_to_timeline_media.edges", default=[])
        if isinstance(timeline_edges, list):
            for edge in timeline_edges[: self.max_recent_posts]:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                shortcode = normalize_text(node.get("shortcode"))
                if not shortcode:
                    continue
                recent_posts.append(f"https://www.instagram.com/p/{shortcode}/")
                prefetched_nodes[shortcode] = {"node": node, "source": "timeline", "post_type": "post"}

        reel_edges = get_nested_value(user, "edge_felix_video_timeline.edges", default=[])
        if isinstance(reel_edges, list):
            for edge in reel_edges[: self.max_recent_reels]:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                shortcode = normalize_text(node.get("shortcode"))
                if not shortcode:
                    continue
                recent_reels.append(f"https://www.instagram.com/reel/{shortcode}/")
                prefetched_nodes[shortcode] = {"node": node, "source": "reel_timeline", "post_type": "reel"}

        return (
            unique_preserve_order(recent_posts)[: self.max_recent_posts],
            unique_preserve_order(recent_reels)[: self.max_recent_reels],
            prefetched_nodes,
        )

    def _extract_post_data(self, response: Response, username: str) -> dict[str, Any]:
        post_item = self._build_empty_post_item(response.url, username)
        json_ld_objects, script_objects = self._extract_page_json(response)

        media_node = self._select_media_node(script_objects, post_item["shortcode"])
        json_ld_post = self._select_json_ld_post(json_ld_objects)

        if isinstance(media_node, dict):
            media_shortcode = normalize_text(media_node.get("shortcode") or media_node.get("code"))
            if media_shortcode:
                post_item["shortcode"] = media_shortcode

            post_item["caption"] = self._first_non_null(
                [
                    self._extract_caption_from_media(media_node),
                    post_item["caption"],
                ]
            )
            post_item["published_at"] = self._first_non_null(
                [
                    to_iso_datetime(
                        self._first_non_null(
                            [
                                media_node.get("taken_at_timestamp"),
                                media_node.get("taken_at"),
                                media_node.get("created_at"),
                            ]
                        )
                    ),
                    post_item["published_at"],
                ]
            )

            likes = self._as_int(
                self._first_non_null(
                    [
                        get_nested_value(media_node, "edge_media_preview_like.count"),
                        get_nested_value(media_node, "edge_liked_by.count"),
                        media_node.get("like_count"),
                    ]
                )
            )
            comments_count = self._as_int(
                self._first_non_null(
                    [
                        get_nested_value(media_node, "edge_media_to_parent_comment.count"),
                        get_nested_value(media_node, "edge_media_to_comment.count"),
                        media_node.get("comment_count"),
                    ]
                )
            )
            views = self._as_int(
                self._first_non_null(
                    [
                        media_node.get("video_view_count"),
                        media_node.get("video_play_count"),
                        media_node.get("view_count"),
                    ]
                )
            )

            post_item["likes"] = likes
            post_item["likes_text"] = str(likes) if likes is not None else None
            post_item["comments_count"] = comments_count
            post_item["comments_count_text"] = str(comments_count) if comments_count is not None else None
            post_item["views"] = views
            post_item["views_text"] = str(views) if views is not None else None
            post_item["owner_username"] = self._first_non_null(
                [
                    normalize_text(get_nested_value(media_node, "owner.username")),
                    normalize_text(get_nested_value(media_node, "user.username")),
                    post_item["owner_username"],
                ]
            )
            post_item["thumbnails_or_media_urls"] = self._extract_media_urls(media_node, response)

        if isinstance(json_ld_post, dict):
            post_item["caption"] = self._first_non_null(
                [post_item["caption"], normalize_text(json_ld_post.get("caption")), normalize_text(json_ld_post.get("description"))]
            )
            post_item["published_at"] = self._first_non_null(
                [
                    post_item["published_at"],
                    to_iso_datetime(json_ld_post.get("uploadDate")),
                    to_iso_datetime(json_ld_post.get("datePublished")),
                ]
            )
            if post_item["owner_username"] is None:
                author = json_ld_post.get("author")
                if isinstance(author, dict):
                    author_name = normalize_text(author.get("alternateName") or author.get("name"))
                    if author_name and author_name.startswith("@"):
                        author_name = author_name[1:]
                    post_item["owner_username"] = author_name

        og_description = self._first_non_null(
            [
                normalize_text(response.xpath("//meta[@property='og:description']/@content").get()),
                normalize_text(response.xpath("//meta[@name='description']/@content").get()),
            ]
        )
        if og_description:
            likes_text = self._extract_metric_text(og_description, self.METRIC_LIKES_RE)
            comments_text = self._extract_metric_text(og_description, self.METRIC_COMMENTS_RE)
            views_text = self._extract_metric_text(og_description, self.METRIC_VIEWS_RE)

            post_item["likes_text"] = self._first_non_null([post_item["likes_text"], likes_text])
            post_item["comments_count_text"] = self._first_non_null([post_item["comments_count_text"], comments_text])
            post_item["views_text"] = self._first_non_null([post_item["views_text"], views_text])

            post_item["likes"] = self._first_non_null([post_item["likes"], self._as_int(likes_text)])
            post_item["comments_count"] = self._first_non_null([post_item["comments_count"], self._as_int(comments_text)])
            post_item["views"] = self._first_non_null([post_item["views"], self._as_int(views_text)])

            if post_item["caption"] is None:
                post_item["caption"] = self._extract_caption_from_description(og_description)
            if post_item["owner_username"] is None:
                handle_match = self.USERNAME_HANDLE_RE.search(og_description)
                if handle_match:
                    post_item["owner_username"] = handle_match.group(1)

        if post_item["views"] is None:
            jsonld_views = self._extract_json_ld_interaction_count(json_ld_objects, ("watch", "view"))
            post_item["views"] = jsonld_views
            if post_item["views_text"] is None and jsonld_views is not None:
                post_item["views_text"] = str(jsonld_views)
        if post_item["comments_count"] is None:
            jsonld_comments = self._extract_json_ld_interaction_count(json_ld_objects, ("comment",))
            post_item["comments_count"] = jsonld_comments
            if post_item["comments_count_text"] is None and jsonld_comments is not None:
                post_item["comments_count_text"] = str(jsonld_comments)
        if post_item["likes"] is None:
            jsonld_likes = self._extract_json_ld_interaction_count(json_ld_objects, ("like",))
            post_item["likes"] = jsonld_likes
            if post_item["likes_text"] is None and jsonld_likes is not None:
                post_item["likes_text"] = str(jsonld_likes)

        comments = []
        if self.scrape_comments:
            try:
                comments = self._extract_comments(response, media_node, post_item["owner_username"])
            except Exception:
                self.logger.warning("Comment extraction failure on %s", response.url, exc_info=True)
                comments = []

        post_item["comments"] = comments
        post_item["scraped_at"] = utc_now_iso()
        return post_item

    def _extract_comments(self, response: Response, media_node: dict[str, Any] | None, owner_username: str | None) -> list[dict[str, Any]]:
        comments = self._extract_comments_from_media_node(media_node, owner_username)
        if not comments:
            comments = self._extract_comments_from_html(response, owner_username)
        comments = self._dedupe_comments(comments)
        return comments[: self.max_visible_comments]

    def _extract_comments_from_media_node(
        self, media_node: dict[str, Any] | None, owner_username: str | None
    ) -> list[dict[str, Any]]:
        if not isinstance(media_node, dict):
            return []

        comments: list[dict[str, Any]] = []
        edges = self._first_non_null(
            [
                get_nested_value(media_node, "edge_media_to_parent_comment.edges"),
                get_nested_value(media_node, "edge_media_to_comment.edges"),
            ]
        )

        if isinstance(edges, list):
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                comment = self._build_comment_object(node)
                if not comment:
                    continue

                replies = []
                reply_edges = get_nested_value(node, "edge_threaded_comments.edges", default=[])
                if isinstance(reply_edges, list):
                    for reply_edge in reply_edges:
                        reply_node = reply_edge.get("node") if isinstance(reply_edge, dict) else None
                        reply_comment = self._build_comment_object(reply_node)
                        if not reply_comment:
                            continue
                        reply_comment["reply_count"] = None
                        reply_comment["owner_replied"] = None
                        reply_comment["replies"] = []
                        replies.append(reply_comment)

                comment["replies"] = self._dedupe_comments(replies)
                if comment.get("reply_count") is None and comment["replies"]:
                    comment["reply_count"] = len(comment["replies"])
                if owner_username:
                    comment["owner_replied"] = any(
                        (reply.get("username") or "").lower() == owner_username.lower()
                        for reply in comment["replies"]
                    )
                comments.append(comment)
                if len(comments) >= self.max_visible_comments:
                    break

        if comments:
            return comments

        preview_comments = self._first_non_null(
            [
                get_nested_value(media_node, "edge_media_to_parent_comment.preview_comments"),
                get_nested_value(media_node, "edge_media_to_comment.preview_comments"),
            ]
        )
        if not isinstance(preview_comments, list):
            return []

        for node in preview_comments:
            comment = self._build_comment_object(node)
            if comment:
                comments.append(comment)
            if len(comments) >= self.max_visible_comments:
                break
        return comments

    def _extract_comments_from_html(self, response: Response, owner_username: str | None) -> list[dict[str, Any]]:
        comments = self._extract_comments_from_legacy_html(response, owner_username)
        if comments:
            return comments
        return self._extract_comments_from_rendered_dom(response, owner_username)

    def _extract_comments_from_legacy_html(self, response: Response, owner_username: str | None) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        nodes = response.xpath("//ul//li[.//h3]")

        for node in nodes:
            username = normalize_text(node.xpath(".//h3//text()").get())
            text_candidates = [normalize_text(value) for value in node.xpath(".//span//text()").getall()]
            text_candidates = [value for value in text_candidates if value]
            comment_text = None
            for value in text_candidates:
                lowered = value.lower()
                if username and value == username:
                    continue
                if lowered in {"like", "reply", "see translation", "view replies", "view more replies"}:
                    continue
                comment_text = value
                break

            if not username and not comment_text:
                continue

            node_text_blob = normalize_text(" ".join(node.xpath(".//text()").getall())) or ""
            likes_text = self._extract_metric_text(node_text_blob, self.METRIC_LIKES_RE)
            likes = self._as_int(likes_text)
            published_at = to_iso_datetime(node.xpath(".//time/@datetime").get())

            comment = {
                "comment_id": None,
                "username": username,
                "text": comment_text,
                "published_at": published_at,
                "likes_text": likes_text,
                "likes": likes,
                "is_pinned": True if "pinned" in node_text_blob.lower() else None,
                "reply_count": None,
                "owner_replied": None,
                "replies": [],
                "scraped_at": utc_now_iso(),
            }

            if owner_username and username and username.lower() == owner_username.lower():
                comment["owner_replied"] = True

            comments.append(comment)
            if len(comments) >= self.max_visible_comments:
                break
        return comments

    def _extract_comments_from_rendered_dom(self, response: Response, owner_username: str | None) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        root = getattr(response.selector, "root", None)
        if root is None:
            return comments

        comments = self._extract_comments_from_rendered_comment_rows(root, owner_username)
        if len(comments) >= self.max_visible_comments:
            return comments[: self.max_visible_comments]

        # Fallback heuristic for layouts where list item rows are hard to isolate.
        fallback_comments = self._extract_comments_from_rendered_anchor_scan(root, owner_username)
        merged = self._dedupe_comments(comments + fallback_comments)
        return merged[: self.max_visible_comments]

    def _extract_comments_from_rendered_comment_rows(self, root, owner_username: str | None) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        seen_rows: set[int] = set()

        # Modern Instagram comments are usually under li rows in the right-side panel.
        comment_rows = root.xpath("//li[.//a[@href] and .//*[contains(translate(normalize-space(.), 'REPLY', 'reply'), 'reply')]]")
        for row in comment_rows:
            row_id = id(row)
            if row_id in seen_rows:
                continue
            seen_rows.add(row_id)

            username = self._extract_username_from_row(row)
            if not username:
                continue

            collected_texts = [normalize_text(text) for text in row.xpath(".//text()")]
            collected_texts = [text for text in collected_texts if text]
            if not collected_texts:
                continue

            row_text_blob = normalize_text(" ".join(collected_texts)) or ""
            lowered_blob = row_text_blob.lower()
            comment_text = self._extract_comment_text_from_row_texts(collected_texts=collected_texts, username=username)
            if not comment_text:
                continue
            if self._is_owner_caption_like_row(owner_username, username, comment_text, lowered_blob):
                continue

            likes_text = self._extract_metric_text(row_text_blob, self.METRIC_LIKES_RE)
            likes = self._as_int(likes_text)
            reply_count = self._extract_reply_count_from_text(row_text_blob)
            published_at = to_iso_datetime(row.xpath(".//time/@datetime").get())

            comment = {
                "comment_id": None,
                "username": username,
                "text": comment_text,
                "published_at": published_at,
                "likes_text": likes_text,
                "likes": likes,
                "is_pinned": True if "pinned" in lowered_blob else None,
                "reply_count": reply_count,
                "owner_replied": None,
                "replies": [],
                "scraped_at": utc_now_iso(),
            }
            comments.append(comment)
            if len(comments) >= self.max_visible_comments:
                break

        return comments

    def _extract_comments_from_rendered_anchor_scan(self, root, owner_username: str | None) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        seen_rows: set[int] = set()
        anchors = root.xpath("//a[@href]")
        for anchor in anchors:
            href = normalize_text(anchor.get("href"))
            if not href:
                continue
            username_match = self.USERNAME_PATH_RE.match(href)
            if not username_match:
                continue
            username = normalize_text(username_match.group(1))
            if not username:
                continue

            row = anchor
            candidate_row = None
            collected_texts: list[str] = []
            for _ in range(10):
                row = row.getparent()
                if row is None:
                    break
                texts = [normalize_text(text) for text in row.xpath(".//text()")]
                texts = [text for text in texts if text]
                lowered = {text.lower() for text in texts}
                if len(texts) >= 4 and "like" in lowered and "reply" in lowered:
                    candidate_row = row
                    collected_texts = texts
                    break

            if candidate_row is None:
                continue
            row_id = id(candidate_row)
            if row_id in seen_rows:
                continue
            seen_rows.add(row_id)

            row_text_blob = normalize_text(" ".join(collected_texts)) or ""
            lowered_blob = row_text_blob.lower()
            likes_text = self._extract_metric_text(row_text_blob, self.METRIC_LIKES_RE)
            likes = self._as_int(likes_text)
            reply_count = self._extract_reply_count_from_text(row_text_blob)

            published_at = to_iso_datetime(
                self._first_non_null(
                    [
                        normalize_text(candidate_row.xpath(".//time/@datetime")[0])
                        if candidate_row.xpath(".//time/@datetime")
                        else None,
                    ]
                )
            )

            comment_text = self._extract_comment_text_from_row_texts(
                collected_texts=collected_texts,
                username=username,
            )
            if not comment_text:
                continue
            if self._is_owner_caption_like_row(owner_username, username, comment_text, lowered_blob):
                continue

            comment = {
                "comment_id": None,
                "username": username,
                "text": comment_text,
                "published_at": published_at,
                "likes_text": likes_text,
                "likes": likes,
                "is_pinned": True if "pinned" in lowered_blob else None,
                "reply_count": reply_count,
                "owner_replied": None,
                "replies": [],
                "scraped_at": utc_now_iso(),
            }

            comments.append(comment)
            if len(comments) >= self.max_visible_comments:
                break
        return comments

    def _extract_username_from_row(self, row) -> str | None:
        anchors = row.xpath(".//a[@href]")
        for anchor in anchors:
            href = normalize_text(anchor.get("href"))
            if not href:
                continue
            username_match = self.USERNAME_PATH_RE.match(href)
            if not username_match:
                continue
            username = normalize_text(username_match.group(1))
            if username:
                return username
        return None

    def _is_owner_caption_like_row(
        self,
        owner_username: str | None,
        username: str | None,
        comment_text: str | None,
        row_text_blob_lower: str,
    ) -> bool:
        if not owner_username or not username:
            return False
        if username.lower() != owner_username.lower():
            return False
        text = (comment_text or "").strip().lower()
        if text in {"original audio", "audio is muted"}:
            return True
        if "original audio" in row_text_blob_lower or "audio is muted" in row_text_blob_lower:
            return True
        return False

    def _extract_comment_text_from_row_texts(self, collected_texts: list[str], username: str) -> str | None:
        ignore_tokens = {
            "like",
            "reply",
            "see translation",
            "hide",
            "report",
            "delete",
            "edited",
            "send message",
            "follow",
            "following",
            "verified",
            "audio is muted",
            "original audio",
            "more",
            "add a comment...",
        }
        username_lower = username.lower()

        for text in collected_texts:
            lowered = text.lower()
            if lowered == username_lower:
                continue
            if lowered in ignore_tokens:
                continue
            if lowered.startswith("view") and "repl" in lowered:
                continue
            if re.match(r"^\d+[smhdw]$", lowered) or re.match(r"^\d+[smhdw]\s+ago$", lowered):
                continue
            if re.match(r"^\d+[.,]?\d*\s+likes?$", lowered):
                continue
            if text in {"•", "·"}:
                continue
            return text
        return None

    def _extract_reply_count_from_text(self, text: str) -> int | None:
        match = re.search(r"view(?: all)?\s+(\d+)\s+repl", text, flags=re.IGNORECASE)
        if not match:
            return None
        return self._as_int(match.group(1))

    def _build_comment_object(self, node: Any) -> dict[str, Any] | None:
        if not isinstance(node, dict):
            return None

        username = self._first_non_null(
            [
                normalize_text(get_nested_value(node, "owner.username")),
                normalize_text(get_nested_value(node, "user.username")),
                normalize_text(node.get("username")),
            ]
        )
        text = self._first_non_null([normalize_text(node.get("text")), normalize_text(node.get("body"))])
        if not username and not text:
            return None

        likes = self._as_int(
            self._first_non_null(
                [
                    get_nested_value(node, "edge_liked_by.count"),
                    node.get("like_count"),
                    node.get("comment_like_count"),
                ]
            )
        )
        is_pinned_raw = self._first_non_null([node.get("is_pinned"), node.get("pinned")])
        is_pinned = is_pinned_raw if isinstance(is_pinned_raw, bool) else None
        reply_count = self._as_int(
            self._first_non_null([get_nested_value(node, "edge_threaded_comments.count"), node.get("reply_count")])
        )

        return {
            "comment_id": normalize_text(node.get("id")),
            "username": username,
            "text": text,
            "published_at": to_iso_datetime(
                self._first_non_null([node.get("created_at"), node.get("created_time"), node.get("timestamp")])
            ),
            "likes_text": str(likes) if likes is not None else None,
            "likes": likes,
            "is_pinned": is_pinned,
            "reply_count": reply_count,
            "owner_replied": None,
            "replies": [],
            "scraped_at": utc_now_iso(),
        }

    def _extract_media_urls(self, media_node: dict[str, Any], response: Response | None = None) -> list[str]:
        urls: list[str] = []
        candidates: list[str | None] = [
            media_node.get("display_url"),
            media_node.get("thumbnail_src"),
            media_node.get("video_url"),
        ]

        if response is not None:
            candidates.extend(
                [
                    response.xpath("//meta[@property='og:image']/@content").get(),
                    response.xpath("//meta[@property='og:video']/@content").get(),
                ]
            )

        display_resources = media_node.get("display_resources")
        if isinstance(display_resources, list):
            for resource in display_resources:
                if isinstance(resource, dict):
                    candidates.append(resource.get("src"))

        sidecar_items = media_node.get("edge_sidecar_to_children", {}).get("edges", [])
        if isinstance(sidecar_items, list):
            for edge in sidecar_items:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    candidates.append(node.get("display_url"))
                    candidates.append(node.get("video_url"))

        for candidate in candidates:
            normalized = normalize_text(candidate)
            if not normalized:
                continue
            if normalized.startswith("//"):
                normalized = "https:" + normalized
            urls.append(normalized)
        return unique_preserve_order(urls)

    def _extract_caption_from_media(self, media_node: dict[str, Any]) -> str | None:
        caption_edges = get_nested_value(media_node, "edge_media_to_caption.edges", default=[])
        if isinstance(caption_edges, list):
            for edge in caption_edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                text = normalize_text(node.get("text")) if isinstance(node, dict) else None
                if text:
                    return text
        return self._first_non_null([normalize_text(media_node.get("title")), normalize_text(media_node.get("caption"))])

    def _extract_caption_from_description(self, description: str) -> str | None:
        if " - " in description:
            maybe_caption = normalize_text(description.split(" - ", 1)[1])
            if maybe_caption:
                return maybe_caption
        if ": " in description:
            maybe_caption = normalize_text(description.split(": ", 1)[1])
            if maybe_caption:
                return maybe_caption
        return None

    def _extract_metric_text(self, text: str, pattern: re.Pattern[str]) -> str | None:
        if not text:
            return None
        match = pattern.search(text)
        if not match:
            return None
        return normalize_text(match.group(1))

    def _extract_profile_counts(self, text: str) -> dict[str, Any]:
        followers_text = self._extract_metric_text(text, self.PROFILE_FOLLOWERS_RE)
        following_text = self._extract_metric_text(text, self.PROFILE_FOLLOWING_RE)
        posts_text = self._extract_metric_text(text, self.PROFILE_POSTS_RE)

        return {
            "followers_text": followers_text,
            "following_text": following_text,
            "posts_text": posts_text,
            "followers": self._as_int(followers_text),
            "following": self._as_int(following_text),
            "posts": self._as_int(posts_text),
        }

    def _augment_recent_posts_with_reel_equivalents(
        self, recent_posts: list[str], recent_reels: list[str]
    ) -> list[str]:
        merged_posts = list(recent_posts or [])
        for reel_url in recent_reels or []:
            shortcode = extract_shortcode_from_url(reel_url)
            if not self._is_valid_shortcode(shortcode):
                continue
            merged_posts.append(f"https://www.instagram.com/p/{shortcode}/")

        max_combined = max(1, self.max_recent_posts + self.max_recent_reels)
        return unique_preserve_order(merged_posts)[:max_combined]

    def _is_valid_shortcode(self, shortcode: Any) -> bool:
        value = normalize_text(shortcode)
        if not value:
            return False
        if not self.SHORTCODE_LIKE_RE.match(value):
            return False
        if self.LOCALE_CODE_RE.match(value):
            return False
        return True

    def _extract_recent_urls(
        self, response: Response, script_objects: list[Any] | None = None
    ) -> tuple[list[str], list[str]]:
        post_urls: list[str] = []
        reel_urls: list[str] = []

        hrefs = response.css("a[href*='/p/']::attr(href), a[href*='/reel/']::attr(href)").getall()
        for href in hrefs:
            full_url = canonicalize_instagram_url(href)
            if not full_url:
                continue
            shortcode = extract_shortcode_from_url(full_url)
            if not self._is_valid_shortcode(shortcode):
                continue
            if "/p/" in full_url:
                post_urls.append(full_url)
            elif "/reel/" in full_url:
                reel_urls.append(full_url)

        for kind, shortcode in self.URL_DISCOVERY_RE.findall(response.text):
            if not self._is_valid_shortcode(shortcode):
                continue
            discovered = f"https://www.instagram.com/{kind}/{shortcode}/"
            if kind == "p":
                post_urls.append(discovered)
            else:
                reel_urls.append(discovered)

        for kind, shortcode in self.URL_DISCOVERY_ESCAPED_RE.findall(response.text):
            if not self._is_valid_shortcode(shortcode):
                continue
            discovered = f"https://www.instagram.com/{kind}/{shortcode}/"
            if kind == "p":
                post_urls.append(discovered)
            else:
                reel_urls.append(discovered)

        # Some responses include shortcode JSON blobs without full /p/ or /reel/ URLs.
        for shortcode in self.SHORTCODE_JSON_RE.findall(response.text):
            if self._is_valid_shortcode(shortcode):
                post_urls.append(f"https://www.instagram.com/p/{shortcode}/")
        for shortcode in self.SHORTCODE_ESCAPED_JSON_RE.findall(response.text):
            if self._is_valid_shortcode(shortcode):
                post_urls.append(f"https://www.instagram.com/p/{shortcode}/")
        for shortcode in self.CODE_JSON_RE.findall(response.text):
            if self._is_valid_shortcode(shortcode):
                post_urls.append(f"https://www.instagram.com/p/{shortcode}/")
        for shortcode in self.CODE_ESCAPED_JSON_RE.findall(response.text):
            if self._is_valid_shortcode(shortcode):
                post_urls.append(f"https://www.instagram.com/p/{shortcode}/")

        pw_posts, pw_reels = self._extract_recent_urls_from_playwright_meta(response)
        post_urls.extend(pw_posts)
        reel_urls.extend(pw_reels)

        script_posts, script_reels = self._extract_recent_urls_from_script_objects(script_objects or [])
        post_urls.extend(script_posts)
        reel_urls.extend(script_reels)

        return (
            unique_preserve_order(post_urls)[: self.max_recent_posts],
            unique_preserve_order(reel_urls)[: self.max_recent_reels],
        )

    def _extract_recent_urls_from_playwright_meta(self, response: Response) -> tuple[list[str], list[str]]:
        post_urls: list[str] = []
        reel_urls: list[str] = []

        methods = response.meta.get("playwright_page_methods")
        if not isinstance(methods, list):
            return post_urls, reel_urls

        for method in methods:
            result = getattr(method, "result", None)
            if not isinstance(result, list):
                continue
            for href in result:
                full_url = canonicalize_instagram_url(href)
                if not full_url:
                    continue
                shortcode = extract_shortcode_from_url(full_url)
                if not self._is_valid_shortcode(shortcode):
                    continue
                if "/p/" in full_url:
                    post_urls.append(full_url)
                elif "/reel/" in full_url:
                    reel_urls.append(full_url)

        return post_urls, reel_urls

    def _extract_recent_urls_from_script_objects(self, script_objects: list[Any]) -> tuple[list[str], list[str]]:
        post_urls: list[str] = []
        reel_urls: list[str] = []

        for obj in script_objects:
            for node in iter_dicts(obj):
                if not isinstance(node, dict):
                    continue

                permalink = canonicalize_instagram_url(
                    self._first_non_null([node.get("permalink"), node.get("url")])
                )
                permalink_shortcode = extract_shortcode_from_url(permalink) if permalink else None
                if permalink and self._is_valid_shortcode(permalink_shortcode) and "/reel/" in permalink:
                    reel_urls.append(permalink)
                    continue
                if permalink and self._is_valid_shortcode(permalink_shortcode) and "/p/" in permalink:
                    post_urls.append(permalink)
                    continue

                shortcode = normalize_text(node.get("shortcode") or node.get("code"))
                if not self._is_valid_shortcode(shortcode):
                    continue
                if not self._looks_like_media_node(node):
                    continue

                is_reel = False
                product_type = normalize_text(node.get("product_type"))
                typename = normalize_text(node.get("__typename"))
                media_type = self._as_int(node.get("media_type"))
                if isinstance(node.get("is_reel_media"), bool):
                    is_reel = bool(node.get("is_reel_media"))
                if product_type and "clip" in product_type.lower():
                    is_reel = True
                if typename and "reel" in typename.lower():
                    is_reel = True
                if media_type == 2 and product_type and "clips" in product_type.lower():
                    is_reel = True

                if is_reel:
                    reel_urls.append(f"https://www.instagram.com/reel/{shortcode}/")
                else:
                    post_urls.append(f"https://www.instagram.com/p/{shortcode}/")

        return post_urls, reel_urls

    def _looks_like_media_node(self, node: dict[str, Any]) -> bool:
        media_keys = (
            "display_url",
            "thumbnail_src",
            "thumbnail_resources",
            "video_url",
            "video_versions",
            "image_versions2",
            "edge_media_to_caption",
            "edge_media_preview_like",
            "edge_media_to_parent_comment",
            "edge_media_to_comment",
            "taken_at_timestamp",
            "taken_at",
            "media_type",
            "product_type",
            "owner",
        )
        return any(key in node for key in media_keys)

    def _extract_page_json(self, response: Response) -> tuple[list[dict[str, Any]], list[Any]]:
        json_ld_items: list[dict[str, Any]] = []
        json_ld_texts = response.xpath("//script[@type='application/ld+json']/text()").getall()
        for text in json_ld_texts:
            parsed = safe_json_loads(text)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        json_ld_items.append(item)
            elif isinstance(parsed, dict):
                json_ld_items.append(parsed)

        flattened_json_ld = self._flatten_json_objects(json_ld_items)
        script_texts = response.xpath("//script/text()").getall()
        script_objects = extract_json_objects_from_scripts(script_texts)
        return flattened_json_ld, script_objects

    def _flatten_json_objects(self, objects: Iterable[Any]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for obj in objects:
            if isinstance(obj, dict):
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    for graph_item in graph:
                        if isinstance(graph_item, dict):
                            flattened.append(graph_item)
                flattened.append(obj)
            elif isinstance(obj, list):
                for list_item in obj:
                    if isinstance(list_item, dict):
                        flattened.append(list_item)
        return flattened

    def _select_json_ld_person(self, json_ld_objects: list[dict[str, Any]], username: str) -> dict[str, Any] | None:
        lowered_username = username.lower()
        best = None
        best_score = -1

        for obj in json_ld_objects:
            obj_type = normalize_text(obj.get("@type") or "")
            if obj_type and "person" not in obj_type.lower():
                continue

            score = 0
            name = normalize_text(obj.get("name"))
            description = normalize_text(obj.get("description"))
            alternate_name = normalize_text(obj.get("alternateName"))
            url = normalize_text(obj.get("url"))

            if name:
                score += 1
            if description:
                score += 1
            if alternate_name and alternate_name.lstrip("@").lower() == lowered_username:
                score += 3
            if url and f"/{lowered_username}/" in url.lower():
                score += 3

            if score > best_score:
                best = obj
                best_score = score

        return best

    def _select_json_ld_post(self, json_ld_objects: list[dict[str, Any]]) -> dict[str, Any] | None:
        best = None
        best_score = -1
        for obj in json_ld_objects:
            obj_type = normalize_text(obj.get("@type") or "")
            score = 0
            if obj_type and any(kind in obj_type.lower() for kind in ("videoobject", "imageobject", "socialmediaposting")):
                score += 3
            if normalize_text(obj.get("description")):
                score += 1
            if normalize_text(obj.get("uploadDate")) or normalize_text(obj.get("datePublished")):
                score += 1
            if obj.get("interactionStatistic") is not None:
                score += 1
            if score > best_score:
                best = obj
                best_score = score
        return best

    def _select_profile_candidate(self, script_objects: list[Any], username: str) -> dict[str, Any] | None:
        lowered = username.lower()
        best = None
        best_score = -1
        profile_keys = {
            "full_name",
            "biography",
            "profile_pic_url",
            "profile_pic_url_hd",
            "external_url",
            "edge_followed_by",
            "edge_follow",
            "edge_owner_to_timeline_media",
        }

        for obj in script_objects:
            for node in iter_dicts(obj):
                if not isinstance(node, dict):
                    continue
                score = 0
                node_username = normalize_text(node.get("username"))
                if node_username and node_username.lower() == lowered:
                    score += 6
                if any(key in node for key in profile_keys):
                    score += 3
                if isinstance(node.get("edge_followed_by"), dict) and "count" in node["edge_followed_by"]:
                    score += 2
                if isinstance(node.get("edge_follow"), dict) and "count" in node["edge_follow"]:
                    score += 2
                if isinstance(node.get("edge_owner_to_timeline_media"), dict) and "count" in node["edge_owner_to_timeline_media"]:
                    score += 2

                if score > best_score:
                    best = node
                    best_score = score
        return best if best_score >= 3 else None

    def _select_media_node(self, script_objects: list[Any], shortcode: str | None) -> dict[str, Any] | None:
        lowered_shortcode = (shortcode or "").lower()
        best = None
        best_score = -1

        for obj in script_objects:
            for node in iter_dicts(obj):
                if not isinstance(node, dict):
                    continue

                score = 0
                node_shortcode = normalize_text(node.get("shortcode") or node.get("code"))
                if node_shortcode and lowered_shortcode and node_shortcode.lower() == lowered_shortcode:
                    score += 8
                if any(
                    key in node
                    for key in (
                        "display_url",
                        "thumbnail_src",
                        "video_url",
                        "owner",
                        "taken_at_timestamp",
                        "edge_media_to_caption",
                        "edge_media_preview_like",
                        "edge_media_to_parent_comment",
                        "edge_media_to_comment",
                    )
                ):
                    score += 2
                if get_nested_value(node, "owner.username") is not None:
                    score += 1
                if score > best_score:
                    best = node
                    best_score = score
        return best if best_score >= 3 else None

    def _extract_json_ld_interaction_count(
        self, json_ld_objects: list[dict[str, Any]], keywords: tuple[str, ...]
    ) -> int | None:
        for obj in json_ld_objects:
            interaction_stats = obj.get("interactionStatistic")
            if interaction_stats is None:
                continue
            stats_list = interaction_stats if isinstance(interaction_stats, list) else [interaction_stats]
            for stat in stats_list:
                if not isinstance(stat, dict):
                    continue
                interaction_type = normalize_text(stat.get("interactionType") or "")
                if not interaction_type:
                    continue
                lowered = interaction_type.lower()
                if not any(keyword in lowered for keyword in keywords):
                    continue
                count = self._as_int(stat.get("userInteractionCount"))
                if count is not None:
                    return count
        return None

    def _extract_json_ld_external_url(self, json_ld_person: dict[str, Any] | None) -> str | None:
        if not isinstance(json_ld_person, dict):
            return None
        same_as = json_ld_person.get("sameAs")
        if isinstance(same_as, list):
            for value in same_as:
                normalized = normalize_text(value)
                if normalized and "instagram.com" not in normalized.lower():
                    return normalized
        if isinstance(same_as, str) and "instagram.com" not in same_as.lower():
            return normalize_text(same_as)
        return None

    def _extract_json_ld_image_url(self, json_ld_person: dict[str, Any] | None) -> str | None:
        if not isinstance(json_ld_person, dict):
            return None
        image = json_ld_person.get("image")
        if isinstance(image, str):
            return image
        if isinstance(image, dict):
            return normalize_text(image.get("url") or image.get("contentUrl"))
        return None

    def _extract_full_name_from_title(self, title: str | None) -> str | None:
        text = normalize_text(title)
        if not text:
            return None
        text = re.sub(r"\s*(?:\||-|\u2022)\s*Instagram.*$", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\(@[A-Za-z0-9._]+\)", "", text).strip(" -")
        if text.lower() == "instagram":
            return None
        return text or None

    def _needs_profile_playwright_retry(self, profile_item: dict[str, Any]) -> bool:
        signals = [
            profile_item.get("full_name"),
            profile_item.get("bio"),
            profile_item.get("profile_image_url"),
            profile_item.get("followers"),
            profile_item.get("following"),
            profile_item.get("posts"),
            profile_item.get("recent_posts"),
            profile_item.get("recent_reels"),
        ]
        return not any(signals)

    def _needs_profile_api_fallback(self, profile_item: dict[str, Any]) -> bool:
        if self._needs_profile_playwright_retry(profile_item):
            return True
        if not (profile_item.get("recent_posts") or profile_item.get("recent_reels")):
            return True
        if profile_item.get("full_name") in {None, "Instagram"}:
            return True
        return False

    def _needs_post_playwright_retry(self, post_item: dict[str, Any]) -> bool:
        return self._needs_prefetched_post_enrichment(post_item)

    def _needs_comment_playwright_retry(self, post_item: dict[str, Any]) -> bool:
        if not self.scrape_comments:
            return False
        comments = post_item.get("comments") or []
        if comments:
            return False
        post_type = normalize_text(post_item.get("post_type"))
        return post_type in {"post", "reel"}

    def _needs_prefetched_post_enrichment(self, post_item: dict[str, Any]) -> bool:
        signals = [
            post_item.get("caption"),
            post_item.get("likes"),
            post_item.get("comments_count"),
            post_item.get("views"),
            post_item.get("thumbnails_or_media_urls"),
        ]
        return not any(signals)

    def _build_empty_profile_item(self, username: str, profile_url: str) -> dict[str, Any]:
        canonical_url = canonicalize_instagram_url(profile_url) or f"https://www.instagram.com/{username}/"
        return {
            "item_type": "profile",
            "username": username,
            "profile_url": canonical_url,
            "full_name": None,
            "bio": None,
            "external_url": None,
            "category": None,
            "is_verified": None,
            "followers_text": None,
            "following_text": None,
            "posts_text": None,
            "followers": None,
            "following": None,
            "posts": None,
            "profile_image_url": None,
            "recent_posts": [],
            "recent_reels": [],
            "scraped_at": utc_now_iso(),
        }

    def _build_empty_post_item(self, post_url: str, username: str) -> dict[str, Any]:
        canonical_url = canonicalize_instagram_url(post_url) or post_url
        shortcode = extract_shortcode_from_url(canonical_url)
        post_type = "unknown"
        if "/p/" in (canonical_url or ""):
            post_type = "post"
        elif "/reel/" in (canonical_url or ""):
            post_type = "reel"

        return {
            "item_type": "post",
            "username": username,
            "shortcode": shortcode,
            "post_url": canonical_url,
            "post_type": post_type,
            "caption": None,
            "published_at": None,
            "likes_text": None,
            "likes": None,
            "comments_count_text": None,
            "comments_count": None,
            "views_text": None,
            "views": None,
            "thumbnails_or_media_urls": [],
            "owner_username": None,
            "comments": [],
            "scraped_at": utc_now_iso(),
        }

    def _dedupe_comments(self, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()

        for comment in comments:
            key = self._comment_key(comment)
            if key in seen:
                continue
            seen.add(key)

            replies = comment.get("replies")
            if isinstance(replies, list) and replies:
                comment["replies"] = self._dedupe_comments(replies)
            else:
                comment["replies"] = []

            deduped.append(comment)
        return deduped

    def _comment_key(self, comment: dict[str, Any]) -> str:
        comment_id = normalize_text(comment.get("comment_id"))
        if comment_id:
            return f"id:{comment_id}"
        username = (normalize_text(comment.get("username")) or "").lower()
        text = (normalize_text(comment.get("text")) or "").lower()
        published_at = normalize_text(comment.get("published_at")) or ""
        return f"tuple:{username}|{text}|{published_at}"

    def _parse_usernames(self, username: str | None, usernames: str | None) -> list[str]:
        values: list[str] = []
        if username:
            values.append(username)
        if usernames:
            values.extend(usernames.split(","))

        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            candidate = normalize_text(raw)
            if not candidate:
                continue
            candidate = candidate.lstrip("@").strip("/")
            candidate = candidate.lower()
            if candidate and candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        return normalized

    def _parse_bool(self, value: Any) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _safe_instagram_json_payload(self, text: str | None) -> Any:
        raw = (text or "").strip()
        if not raw:
            return None
        for prefix in ("for (;;);", "while(1);"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].lstrip()
                break
        return safe_json_loads(raw)

    def _first_non_null(self, values: Iterable[Any]) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    def _as_int(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return parse_compact_number(value)
