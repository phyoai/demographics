from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from itemadapter import ItemAdapter
from scrapy.exceptions import CloseSpider

from .utils import atomic_write_json, extract_shortcode_from_url, utc_now_iso


class InstagramJsonStoragePipeline:
    def __init__(self, output_path: str):
        self.output_path = Path(output_path)
        self.storage: dict[str, Any] = {}
        self.crawler = None

    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls(output_path=crawler.settings.get("OUTPUT_JSON_PATH", "data/profiles.json"))
        pipeline.crawler = crawler
        return pipeline

    def open_spider(self, spider=None):
        logger = self._get_logger(spider)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.storage = self._load_or_initialize_storage()
        except ValueError as exc:
            logger.error("%s", exc)
            raise CloseSpider("invalid_json_storage")

        self._update_meta()
        self._save_storage(spider, reason="initialization")

    def close_spider(self, spider=None):
        self._update_meta()
        self._save_storage(spider, reason="close_spider")

    def process_item(self, item, spider=None):
        logger = self._get_logger(spider)
        adapter = ItemAdapter(item)
        payload = adapter.asdict()
        item_type = payload.get("item_type")

        if item_type == "profile":
            self._merge_profile(payload, logger)
        elif item_type == "post":
            self._merge_post(payload, logger)
        else:
            logger.warning("Skipping item with unsupported type: %s", item_type)
            return item

        self._update_meta()
        self._save_storage(spider, reason=f"{item_type}_update")
        return item

    def _load_or_initialize_storage(self) -> dict[str, Any]:
        if not self.output_path.exists():
            return self._default_storage()

        try:
            with self.output_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Storage file is corrupted JSON: {self.output_path}. "
                "Fix or remove this file and run again."
            ) from exc
        except OSError as exc:
            raise ValueError(f"Unable to read storage file {self.output_path}: {exc}") from exc

        self._validate_storage_shape(payload)
        return payload

    def _default_storage(self) -> dict[str, Any]:
        return {
            "profiles": {},
            "meta": {
                "last_run_at": None,
                "total_profiles": 0,
                "total_posts": 0,
            },
        }

    def _validate_storage_shape(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"Storage root must be an object in {self.output_path}.")

        profiles = payload.get("profiles")
        meta = payload.get("meta")
        if not isinstance(profiles, dict):
            raise ValueError(f"'profiles' must be an object in {self.output_path}.")
        if not isinstance(meta, dict):
            raise ValueError(f"'meta' must be an object in {self.output_path}.")

        for username, profile_bundle in profiles.items():
            if not isinstance(profile_bundle, dict):
                raise ValueError(f"profiles['{username}'] must be an object in {self.output_path}.")
            profile_section = profile_bundle.get("profile", {})
            posts_section = profile_bundle.get("posts", {})
            if not isinstance(profile_section, dict):
                raise ValueError(f"profiles['{username}']['profile'] must be an object.")
            if not isinstance(posts_section, dict):
                raise ValueError(f"profiles['{username}']['posts'] must be an object.")

    def _ensure_profile_entry(self, username: str) -> dict[str, Any]:
        profiles = self.storage.setdefault("profiles", {})
        entry = profiles.setdefault(username, {"profile": {}, "posts": {}})
        if not isinstance(entry.get("profile"), dict):
            entry["profile"] = {}
        if not isinstance(entry.get("posts"), dict):
            entry["posts"] = {}
        return entry

    def _merge_profile(self, payload: dict[str, Any], logger) -> None:
        username = payload.get("username")
        if not username:
            logger.warning("Profile item skipped because username is missing.")
            return

        entry = self._ensure_profile_entry(username)
        merged_profile = {k: v for k, v in payload.items() if k != "item_type"}
        entry["profile"] = merged_profile

    def _merge_post(self, payload: dict[str, Any], logger) -> None:
        username = payload.get("username")
        if not username:
            logger.warning("Post item skipped because username is missing.")
            return

        shortcode = payload.get("shortcode") or extract_shortcode_from_url(payload.get("post_url"))
        if not shortcode:
            logger.warning("Post item skipped because shortcode is missing for username=%s.", username)
            return

        entry = self._ensure_profile_entry(username)
        merged_post = {k: v for k, v in payload.items() if k != "item_type"}
        merged_post["shortcode"] = shortcode
        if merged_post.get("comments") is None:
            merged_post["comments"] = []
        entry["posts"][shortcode] = merged_post

    def _update_meta(self) -> None:
        profiles = self.storage.setdefault("profiles", {})
        meta = self.storage.setdefault("meta", {})
        total_posts = 0
        for profile_bundle in profiles.values():
            posts = {}
            if isinstance(profile_bundle, dict):
                posts = profile_bundle.get("posts", {})
            if isinstance(posts, dict):
                total_posts += len(posts)

        meta["last_run_at"] = utc_now_iso()
        meta["total_profiles"] = len(profiles)
        meta["total_posts"] = total_posts

    def _save_storage(self, spider, reason: str) -> None:
        logger = self._get_logger(spider)
        atomic_write_json(self.output_path, self.storage)
        logger.info("JSON storage updated (%s): %s", reason, self.output_path)

    def _get_logger(self, spider=None):
        if spider is not None:
            return spider.logger
        crawler_spider = getattr(getattr(self, "crawler", None), "spider", None)
        if crawler_spider is not None:
            return crawler_spider.logger
        return logging.getLogger(self.__class__.__name__)
