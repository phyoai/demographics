from __future__ import annotations

import json
import os
import re
from typing import Any

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - dependency is optional in some environments
    OpenAI = None


class InstagramProfileLLMAnalyzer:
    """Analyze an Instagram profile with OpenAI and return normalized metadata."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model or os.getenv("INSTAGRAM_PROFILE_ANALYZER_MODEL", "gpt-5.4-mini")
        self.timeout_seconds = timeout_seconds or self._env_float(
            "INSTAGRAM_PROFILE_ANALYZER_TIMEOUT_SECONDS",
            default=20.0,
        )

        if client is not None:
            self.client = client
            return

        if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
            self.client = None
            return

        self.client = OpenAI(max_retries=0)

    @property
    def is_enabled(self) -> bool:
        return self.client is not None

    def analyze(self, document: dict[str, Any]) -> dict[str, Any]:
        profile_payload = self._build_profile_payload(document)
        fallback = self._fallback_result(profile_payload)

        if not self.client:
            return fallback

        prompt = self._build_prompt(profile_payload)

        try:
            response = self.client.with_options(
                timeout=self.timeout_seconds,
                max_retries=0,
            ).responses.create(
                model=self.model,
                instructions=(
                    "You analyze Instagram profiles. Return only valid JSON. "
                    "Do not invent facts. Use null when evidence is weak."
                ),
                input=prompt,
            )
            parsed = self._parse_response_text(response.output_text)
        except Exception:
            return fallback

        result = self._normalize_result(parsed, profile_payload)
        result["analysis_source"] = "openai_llm_profile_analysis"
        result["analysis_model"] = self.model
        return result

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default
        return max(0.1, value)

    @staticmethod
    def _clean_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return re.sub(r"\s+", " ", value).strip()

    def _build_profile_payload(self, document: dict[str, Any]) -> dict[str, Any]:
        result = document.get("result", {})
        if not isinstance(result, dict):
            result = {}

        profile = result.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}

        posts = result.get("posts", [])
        if not isinstance(posts, list):
            posts = []

        sample_posts: list[dict[str, str]] = []
        for post in posts[:5]:
            if not isinstance(post, dict):
                continue

            caption = self._clean_text(post.get("caption") or post.get("alt") or "")
            location = self._clean_text(post.get("location") or post.get("location_name") or "")
            post_url = self._clean_text(post.get("post_url") or "")

            if not caption and not location and not post_url:
                continue

            sample_posts.append(
                {
                    "caption": caption[:400],
                    "location": location[:120],
                    "post_url": post_url[:200],
                }
            )

        raw_external_links = profile.get("external_links", []) or []
        if isinstance(raw_external_links, str):
            raw_external_links = [raw_external_links]
        elif not isinstance(raw_external_links, list):
            raw_external_links = []

        external_links = []
        for link in raw_external_links:
            if isinstance(link, str):
                cleaned_link = self._clean_text(link)
                if cleaned_link:
                    external_links.append(cleaned_link[:200])

        return {
            "requested_username": self._clean_text(document.get("requested_username")),
            "username": self._clean_text(profile.get("username")),
            "full_name": self._clean_text(profile.get("full_name")),
            "bio": self._clean_text(profile.get("bio") or profile.get("biography")),
            "category": self._clean_text(profile.get("category")),
            "business_or_creator_label": self._clean_text(profile.get("business_or_creator_label")),
            "external_url": self._clean_text(profile.get("external_url")),
            "external_links": external_links,
            "followers_count": profile.get("followers_count"),
            "following_count": profile.get("following_count"),
            "posts_count": profile.get("posts_count"),
            "is_verified": bool(profile.get("is_verified")),
            "is_private": bool(profile.get("is_private")),
            "profile_url": self._clean_text(profile.get("profile_url")),
            "sample_posts": sample_posts,
        }

    def _build_prompt(self, profile_payload: dict[str, Any]) -> str:
        serialized_payload = json.dumps(profile_payload, ensure_ascii=True, indent=2)
        return f"""
Analyze this Instagram profile payload and infer likely public-facing profile metadata.

Requirements:
- Predict the most likely `country` and `city` for the profile owner.
- Predict `niche` and `category` as short lists.
- Include multiple niches/categories when the profile clearly spans more than one area.
- Keep each niche/category label concise and specific.
- Write a concise `profile_summary` that describes the account's likely focus, audience, and brand positioning.
- Use only the provided evidence.
- If city or country cannot be inferred with reasonable confidence, set them to null.
- Keep `profile_summary` under 50 words.
- Return only JSON with this exact schema:
{{
  "country": "string or null",
  "city": "string or null",
  "niche": ["string"],
  "category": ["string"],
  "profile_summary": "string",
  "confidence_notes": ["short strings"]
}}

Profile payload:
{serialized_payload}
""".strip()

    @staticmethod
    def _parse_response_text(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            cleaned = re.sub(r",\s*}", "}", cleaned)
            cleaned = re.sub(r",\s*]", "]", cleaned)
            parsed = json.loads(cleaned)

        if not isinstance(parsed, dict):
            raise ValueError("OpenAI response must be a JSON object")
        return parsed

    def _normalize_text_list(self, value: Any) -> list[str]:
        items: list[str] = []

        if isinstance(value, str):
            candidates = re.split(r"[|,/;]+", value)
        elif isinstance(value, list):
            candidates = value
        else:
            return items

        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue

            cleaned = self._clean_text(candidate)[:80]
            if not cleaned:
                continue

            key = cleaned.lower()
            if key in seen:
                continue

            seen.add(key)
            items.append(cleaned)

        return items[:8]

    def _normalize_result(
        self,
        result: dict[str, Any],
        profile_payload: dict[str, Any],
    ) -> dict[str, Any]:
        fallback = self._fallback_result(profile_payload)
        normalized = dict(fallback)

        for key in ("country", "city", "niche", "category"):
            if key in {"niche", "category"}:
                normalized[key] = self._normalize_text_list(result.get(key)) or fallback[key]
                continue

            value = result.get(key)
            if isinstance(value, str):
                cleaned = self._clean_text(value)
                normalized[key] = cleaned or fallback[key]

        summary = result.get("profile_summary")
        if isinstance(summary, str):
            normalized["profile_summary"] = self._clean_text(summary)[:280] or fallback["profile_summary"]

        confidence_notes = result.get("confidence_notes")
        if isinstance(confidence_notes, list):
            normalized["confidence_notes"] = [
                self._clean_text(item)[:120]
                for item in confidence_notes
                if isinstance(item, str) and self._clean_text(item)
            ][:5]

        return normalized

    @staticmethod
    def _fallback_result(profile_payload: dict[str, Any]) -> dict[str, Any]:
        existing_category = profile_payload.get("category") or profile_payload.get("business_or_creator_label") or None
        username = profile_payload.get("username") or profile_payload.get("requested_username") or "this profile"

        summary = "Insufficient evidence to generate a reliable profile summary."
        if existing_category:
            summary = f"{username} appears to be associated with {existing_category.lower()} content."

        return {
            "country": None,
            "city": None,
            "niche": [],
            "category": [existing_category] if existing_category else [],
            "profile_summary": summary,
            "confidence_notes": [],
            "analysis_source": "fallback_profile_metadata",
            "analysis_model": None,
        }
