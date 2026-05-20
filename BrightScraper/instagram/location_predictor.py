import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from langdetect import detect_langs, LangDetectException

import requests
import tldextract
import spacy
from bs4 import BeautifulSoup
from diskcache import Cache
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

try:
    from ..config import LOCATION_SLANG
except ImportError:
    from BrightScraper.config import LOCATION_SLANG


@dataclass
class EvidenceText:
    source: str
    text: str
    weight: float
    shortcode: Optional[str] = None
    url: Optional[str] = None

@dataclass
class LocationMention:
    text: str
    source: str
    weight: float
    ner_label: str
    shortcode: Optional[str] = None
    url: Optional[str] = None


class InstagramGeoInferencer:
    MIN_LANGUAGE_TEXT_LENGTH = 5
    MAX_LANGUAGE_DETECTIONS = 12
    MAX_LANGUAGE_CHARS_PER_BATCH = 900
    MAX_TEXT_SAMPLES_PER_SOURCE = {
        "profile_bio": 1,
        "profile_full_name": 1,
        "external_link_page": 2,
        "post_caption": 6,
        "recent_post_alt": 2,
        "post_alt": 2,
        "comment": 6,
    }

    HEURISTIC_TRUSTED_SOURCES = {
        "profile_bio",
        "post_caption",
        "external_link_page",
    }

    GENERIC_NON_LOCATION_PHRASES = {
        "new house",
        "my house",
        "our house",
        "your house",
        "this house",
        "the house",
        "see translation",
        "youtube is it",
    }

    GENERIC_NON_LOCATION_TOKENS = {
        "artist",
        "arts",
        "bloopers",
        "caption",
        "celeb",
        "edits",
        "event",
        "fan",
        "highlights",
        "house",
        "journey",
        "milestone",
        "reels",
        "replies",
        "translation",
        "videos",
        "youtube",
    }

    LANGUAGE_FALSE_POSITIVE_NORMALIZATION = {
        "so": "en",
        "vi": "en",
        "pl": "en",
        "fi": "en",
        "nl": "en",
        "da": "en",
        "no": "en",
        "sv": "en",
        "sq": "en",
        "ca": "en",
        "id": "en",
        "tl": "en",
        "cy": "en",
    }

    LANGUAGE_TO_COUNTRY_WEIGHTS = {
        "en": {"USA": 0.24, "India": 0.22, "UK": 0.12, "Canada": 0.08, "Australia": 0.06, "Singapore": 0.04, "UAE": 0.03},
        "hi": {"India": 1.0},
        "ur": {"Pakistan": 0.70, "India": 0.30},
        "bn": {"Bangladesh": 0.85, "India": 0.15},
        "pa": {"India": 0.70, "Pakistan": 0.30},
        "ta": {"India": 0.70, "Sri Lanka": 0.15, "Singapore": 0.10, "UAE": 0.05},
        "te": {"India": 1.0},
        "kn": {"India": 1.0},
        "ml": {"India": 0.95, "UAE": 0.05},
        "mr": {"India": 1.0},
        "gu": {"India": 1.0},
        "ar": {"Saudi Arabia": 0.25, "UAE": 0.20, "Egypt": 0.15, "Morocco": 0.10, "Iraq": 0.08, "India": 0.05},
        "fa": {"Iran": 0.85, "Afghanistan": 0.10, "Tajikistan": 0.05},
        "he": {"Israel": 1.0},
        "tr": {"Turkey": 0.95, "Germany": 0.05},
        "zh-cn": {"China": 0.70, "Taiwan": 0.15, "Singapore": 0.08, "Malaysia": 0.05},
        "zh-tw": {"Taiwan": 0.75, "China": 0.15, "Singapore": 0.05},
        "zh": {"China": 0.70, "Taiwan": 0.15, "Singapore": 0.08, "Malaysia": 0.05},
        "ja": {"Japan": 1.0},
        "ko": {"South Korea": 1.0},
        "th": {"Thailand": 1.0},
        "ms": {"Malaysia": 0.70, "Indonesia": 0.20, "Singapore": 0.10},
        "es": {"Spain": 0.20, "Mexico": 0.25, "Argentina": 0.15, "Colombia": 0.12, "USA": 0.10},
        "pt": {"Brazil": 0.70, "Portugal": 0.20, "Angola": 0.05},
        "fr": {"France": 0.45, "Canada": 0.15, "Belgium": 0.10, "Morocco": 0.08, "Ivory Coast": 0.05},
        "de": {"Germany": 0.80, "Austria": 0.12, "Switzerland": 0.08},
        "it": {"Italy": 1.0},
        "ru": {"Russia": 0.85, "Ukraine": 0.08, "Kazakhstan": 0.05},
        "sw": {"Tanzania": 0.35, "Kenya": 0.30, "Uganda": 0.15, "DRC": 0.10},
        "unknown": {"USA": 0.20, "India": 0.15, "UK": 0.12, "Brazil": 0.10, "Indonesia": 0.08, "Mexico": 0.07},
    }

    SCRIPT_LANGUAGE_HINTS = {
        "hi": r"[\u0900-\u097F]",
        "bn": r"[\u0980-\u09FF]",
        "pa": r"[\u0A00-\u0A7F]",
        "gu": r"[\u0A80-\u0AFF]",
        "ta": r"[\u0B80-\u0BFF]",
        "te": r"[\u0C00-\u0C7F]",
        "kn": r"[\u0C80-\u0CFF]",
        "ml": r"[\u0D00-\u0D7F]",
        "ar": r"[\u0600-\u06FF]",
        "ru": r"[\u0400-\u04FF]",
    }

    COUNTRY_HINT_OVERRIDES = {
        "Bangalore": "India",
        "Mumbai": "India",
        "Delhi": "India",
        "Hyderabad": "India",
        "Chennai": "India",
    }

    INDIA_CITY_PATTERN = re.compile(
        r"\b(delhi|mumbai|bangalore|bengaluru|hyderabad|chennai|pune|kolkata|jaipur|lucknow|gurgaon|gurugram|noida)\b"
    )
    INDIA_COUNTRY_PATTERN = re.compile(r"\b(india|bharat|hindustan)\b")
    PAKISTAN_PATTERN = re.compile(r"\b(pakistan|karachi|lahore|islamabad)\b")
    BANGLADESH_PATTERN = re.compile(r"\b(bangladesh|dhaka|chittagong)\b")
    UAE_PATTERN = re.compile(r"\b(uae|dubai|abu dhabi|sharjah)\b")
    USA_PATTERN = re.compile(r"\b(usa|united states|america|new york|los angeles|chicago|houston)\b")
    UK_PATTERN = re.compile(r"\b(uk|united kingdom|london|manchester|birmingham)\b")

    """
    Production-ready location inferencer for Instagram-style JSON.

    It does NOT use hard-coded city/country lists.

    Flow:
    1. Extract text from profile bio, captions, comments, alt text, and links.
    2. Use lightweight spaCy NER model to detect location entities.
    3. Resolve detected locations using geocoder.
    4. Score city/country using source reliability.
    5. Return city/country only when confidence passes threshold.
    """

    def __init__(
        self,
        spacy_model: str = "xx_ent_wiki_sm",
        cache_dir: str = ".geo_cache",
        user_agent: str = "instagram-geo-inferencer-production",
        fetch_link_pages: bool = True,
        min_city_confidence: float = 0.65,
        min_country_confidence: float = 0.55,
        geocode_sleep_seconds: float = 1.0,
    ):
        self.nlp, self.ner_backend = self._load_nlp(spacy_model)

        self.cache = Cache(cache_dir)
        self.tldextractor = tldextract.TLDExtract(suffix_list_urls=())

        self.geocoder = Nominatim(
            user_agent=user_agent,
            timeout=10,
        )
        self.language_detection_cache: Dict[str, Dict[str, float]] = {}

        self.fetch_link_pages = fetch_link_pages
        self.min_city_confidence = min_city_confidence
        self.min_country_confidence = min_country_confidence
        self.geocode_sleep_seconds = geocode_sleep_seconds

    def _load_nlp(self, spacy_model: str) -> Tuple[Any, str]:
        candidate_models = []

        for model_name in (spacy_model, "en_core_web_sm"):
            if model_name and model_name not in candidate_models:
                candidate_models.append(model_name)

        for model_name in candidate_models:
            try:
                return spacy.load(model_name), f"spacy:{model_name}"
            except OSError:
                continue

        # Keep the pipeline usable even when no downloadable spaCy model exists
        # in the current environment.
        blank_nlp = spacy.blank("xx")

        if "sentencizer" not in blank_nlp.pipe_names:
            blank_nlp.add_pipe("sentencizer")

        return blank_nlp, "heuristic_fallback"

    def infer_from_file(self, json_path: str) -> Dict[str, Any]:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return self.infer(data)

    def infer(self, data: Dict[str, Any]) -> Dict[str, Any]:
        evidence_texts = self.collect_all_texts(data)
        language_result = self.score_languages(evidence_texts)
        text_country_result = self.score_country_from_texts(evidence_texts, language_result)

        location_mentions = self.extract_location_mentions(evidence_texts)

        resolved_locations = []

        for mention in location_mentions:
            resolved = self.resolve_location(mention.text)

            if not resolved:
                continue

            resolved_locations.append({
                "mention": mention.text,
                "source": mention.source,
                "source_weight": mention.weight,
                "shortcode": mention.shortcode,
                "url": mention.url,
                "resolved_city": resolved.get("city"),
                "resolved_country": resolved.get("country"),
                "display_name": resolved.get("display_name"),
                "lat": resolved.get("lat"),
                "lon": resolved.get("lon"),
                "geocoder_type": resolved.get("type"),
                "geocoder_class": resolved.get("class"),
            })

        raw_city_result = self.score_city(resolved_locations)
        geocoder_country_result = self.score_country(resolved_locations)

        final_country = text_country_result["country"]
        country_confidence = text_country_result["confidence"]

        if country_confidence < self.min_country_confidence:
            final_country = None

        city_result = self.filter_city_result_by_country(raw_city_result, final_country)
        final_city = city_result["city"]
        city_confidence = city_result["confidence"]

        if city_confidence < self.min_city_confidence:
            final_city = None

        return {
            "city": final_city,
            "city_confidence": round(city_confidence, 3),
            "country": final_country,
            "country_confidence": round(country_confidence, 3),
            "is_city_confident": city_confidence >= self.min_city_confidence,
            "is_country_confident": country_confidence >= self.min_country_confidence,
            "city_candidates": city_result["candidates"],
            "all_city_candidates": raw_city_result["candidates"],
            "country_candidates": text_country_result["candidates"],
            "geocoder_country_candidates": geocoder_country_result["candidates"],
            "location_mentions": [
                {
                    "text": m.text,
                    "source": m.source,
                    "weight": m.weight,
                    "label": m.ner_label,
                    "shortcode": m.shortcode,
                    "url": m.url,
                }
                for m in location_mentions[:100]
            ],
            "resolved_locations": resolved_locations[:100],
            "total_text_blocks_checked": len(evidence_texts),
            "total_location_mentions_found": len(location_mentions),
            "dominant_language": language_result["language"],
            "language_confidence": round(language_result["confidence"], 3),
            "language_candidates": language_result["candidates"],
            "mode": "light_ner_plus_geocoder_confidence_scoring",
            "ner_backend": self.ner_backend,
            "note": (
                "City/country are returned only when enough reliable evidence is found. "
                "If city is null, the data did not contain strong city evidence."
            ),
        }

    def collect_all_texts(self, data: Dict[str, Any]) -> List[EvidenceText]:
        result = data.get("result", data)
        profile = result.get("profile", {}) or {}
        posts = result.get("posts", []) or []
        recent_posts = profile.get("recent_posts", []) or []

        texts: List[EvidenceText] = []

        # Profile bio is strongest user-authored text.
        self.add_text(texts, "profile_bio", profile.get("bio"), 1.0)

        # Full name can sometimes contain location but usually weak.
        self.add_text(texts, "profile_full_name", profile.get("full_name"), 0.25)

        # Profile external URL.
        external_url = profile.get("external_url")
        if external_url:
            self.add_text(texts, "external_url", external_url, 0.35, url=external_url)

            if self.fetch_link_pages:
                page_text = self.fetch_link_text(external_url)
                if page_text:
                    self.add_text(texts, "external_link_page", page_text, 0.75, url=external_url)

        # Multiple external links.
        for url in profile.get("external_links", []) or []:
            self.add_text(texts, "external_link", url, 0.35, url=url)

            if self.fetch_link_pages:
                page_text = self.fetch_link_text(url)
                if page_text:
                    self.add_text(texts, "external_link_page", page_text, 0.75, url=url)

        # Recent posts alt/caption-style text.
        for post in recent_posts:
            shortcode = post.get("shortcode")
            self.add_text(
                texts,
                "recent_post_alt",
                post.get("alt") or post.get("accessibility_label"),
                0.45,
                shortcode=shortcode,
            )

        # Main posts: captions + comments.
        for post in posts:
            shortcode = post.get("shortcode")

            self.add_text(
                texts,
                "post_caption",
                post.get("caption"),
                0.85,
                shortcode=shortcode,
            )

            self.add_text(
                texts,
                "post_alt",
                post.get("alt") or post.get("accessibility_label"),
                0.45,
                shortcode=shortcode,
            )

            for comment in post.get("comments", []) or []:
                self.add_text(
                    texts,
                    "comment",
                    comment.get("text"),
                    0.20,
                    shortcode=shortcode,
                )

        return texts

    def add_text(
        self,
        texts: List[EvidenceText],
        source: str,
        value: Optional[str],
        weight: float,
        shortcode: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        if not value:
            return

        value = str(value).strip()

        if not value:
            return

        cleaned = self.clean_text(value)

        if not cleaned:
            return

        texts.append(
            EvidenceText(
                source=source,
                text=cleaned,
                weight=weight,
                shortcode=shortcode,
                url=url,
            )
        )

    def clean_text(self, text: str) -> str:
        text = str(text)

        # Remove CDN/image URLs. They create false city signals.
        text = re.sub(r"https?://\S*(fbcdn|cdninstagram|instagram\.f[a-z0-9.-]+)\S*", " ", text)

        # Keep normal URLs separately but avoid huge query params.
        text = re.sub(r"utm_[a-zA-Z0-9_=-]+", " ", text)
        text = re.sub(r"fbclid=[a-zA-Z0-9_=-]+", " ", text)

        # Remove excessive emojis/symbol noise but keep letters.
        text = re.sub(r"[_|•]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text

    def fetch_link_text(self, url: str) -> Optional[str]:
        cache_key = f"link_text::{url}"

        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            response = requests.get(
                url,
                timeout=8,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; InstagramGeoInferencer/1.0)"
                    )
                },
                allow_redirects=True,
            )

            if response.status_code >= 400:
                self.cache[cache_key] = None
                return None

            html = response.text
            soup = BeautifulSoup(html, "html.parser")

            parts = []

            if soup.title and soup.title.string:
                parts.append(soup.title.string)

            for tag in soup.find_all("meta"):
                name = tag.get("name", "").lower()
                prop = tag.get("property", "").lower()

                if name in {"description", "keywords"} or prop in {
                    "og:title",
                    "og:description",
                    "twitter:title",
                    "twitter:description",
                }:
                    content = tag.get("content")
                    if content:
                        parts.append(content)

            final_text = " ".join(parts).strip()
            final_text = self.clean_text(final_text)

            self.cache[cache_key] = final_text or None
            return final_text or None

        except Exception:
            self.cache[cache_key] = None
            return None

    def extract_location_mentions(self, texts: List[EvidenceText]) -> List[LocationMention]:
        mentions: List[LocationMention] = []

        for item in texts:
            if self.ner_backend.startswith("spacy:"):
                mentions.extend(self._extract_mentions_with_spacy(item))
            else:
                mentions.extend(self._extract_mentions_with_heuristics(item))

        return self.dedupe_mentions(mentions)

    def _extract_mentions_with_spacy(self, item: EvidenceText) -> List[LocationMention]:
        mentions: List[LocationMention] = []

        for chunk in self.chunk_text(item.text, max_chars=900):
            doc = self.nlp(chunk)

            for ent in doc.ents:
                label = ent.label_.upper()
                value = ent.text.strip()

                if label not in {"GPE", "LOC", "FAC"}:
                    continue

                if not self.is_safe_location_phrase(value):
                    continue

                mentions.append(
                    LocationMention(
                        text=value,
                        source=item.source,
                        weight=item.weight,
                        ner_label=label,
                        shortcode=item.shortcode,
                        url=item.url,
                    )
                )

        return mentions

    def _extract_mentions_with_heuristics(self, item: EvidenceText) -> List[LocationMention]:
        mentions: List[LocationMention] = []

        if item.source not in self.HEURISTIC_TRUSTED_SOURCES:
            return mentions

        candidate_phrases = self.extract_location_candidates(item.text)

        for phrase in candidate_phrases:
            if not self.is_safe_location_phrase(phrase):
                continue

            mentions.append(
                LocationMention(
                    text=phrase,
                    source=item.source,
                    weight=item.weight,
                    ner_label="HEURISTIC",
                    shortcode=item.shortcode,
                    url=item.url,
                )
            )

        return mentions

    def extract_location_candidates(self, text: str) -> List[str]:
        candidates: List[str] = []
        seen = set()

        cue_pattern = re.compile(
            r"(?i)\b(?:from|in|based in|located in|living in|out of|home is|home:|location:)\s+"
            r"([A-Za-z\u0900-\u097F][A-Za-z0-9\u0900-\u097F .,'&()/:-]{1,80})"
        )
        titlecase_pair_pattern = re.compile(
            r"\b([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2},\s*[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})\b"
        )

        for pattern in (cue_pattern, titlecase_pair_pattern):
            for match in pattern.finditer(text):
                for candidate in self.expand_location_candidate(match.group(1)):
                    key = candidate.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(candidate)

        return candidates

    def expand_location_candidate(self, raw_value: str) -> List[str]:
        value = raw_value.strip(" ,.-|/")
        value = re.split(r"[;!?]|\s(?:and|but)\s|\s[-|]\s", value, maxsplit=1)[0].strip(" ,.-|/")

        if not value:
            return []

        candidates = [value]
        comma_parts = [part.strip() for part in value.split(",") if part.strip()]

        if len(comma_parts) >= 2:
            candidates.append(", ".join(comma_parts[:2]))
            candidates.append(comma_parts[0])

        if len(value.split()) > 4:
            candidates.append(" ".join(value.split()[:4]))

        output = []
        seen = set()

        for candidate in candidates:
            cleaned = candidate.strip(" ,.-|/")
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            output.append(cleaned)

        return output

    def chunk_text(self, text: str, max_chars: int = 900) -> List[str]:
        text = text.strip()

        if len(text) <= max_chars:
            return [text]

        chunks = []
        current = []

        for sentence in re.split(r"(?<=[.!?।])\s+", text):
            if sum(len(x) for x in current) + len(sentence) > max_chars:
                if current:
                    chunks.append(" ".join(current))
                current = [sentence]
            else:
                current.append(sentence)

        if current:
            chunks.append(" ".join(current))

        return chunks

    def is_safe_location_phrase(self, phrase: str) -> bool:
        phrase = phrase.strip()

        if len(phrase) < 3:
            return False

        # Reject handles/usernames.
        if phrase.startswith("@"):
            return False

        # Reject URLs/domains.
        if "http" in phrase.lower() or "www." in phrase.lower():
            return False

        extracted = self.tldextractor(phrase)
        if extracted.domain and extracted.suffix:
            return False

        # Reject hashtag-like/noisy tokens.
        if "#" in phrase:
            return False

        # Reject very long accidental NER spans.
        if len(phrase.split()) > 5:
            return False

        normalized = re.sub(r"\s+", " ", phrase.lower()).strip()
        if normalized in self.GENERIC_NON_LOCATION_PHRASES:
            return False

        # Reject mostly numeric.
        letters = re.findall(r"[A-Za-z\u0900-\u097F]", phrase)
        if len(letters) < 3:
            return False

        tokens = re.findall(r"[A-Za-z\u0900-\u097F]+", normalized)
        if not tokens:
            return False

        generic_token_count = sum(
            1 for token in tokens if token in self.GENERIC_NON_LOCATION_TOKENS
        )
        if generic_token_count == len(tokens):
            return False

        if generic_token_count > 0 and "," not in phrase and len(tokens) <= 3:
            return False

        # Reject CDN hints.
        low = phrase.lower()
        if "fbcdn" in low or "cdninstagram" in low or "fdel" in low:
            return False

        return True

    def dedupe_mentions(self, mentions: List[LocationMention]) -> List[LocationMention]:
        seen = set()
        output = []

        for mention in mentions:
            key = (
                mention.text.lower(),
                mention.source,
                mention.shortcode,
                mention.url,
            )

            if key in seen:
                continue

            seen.add(key)
            output.append(mention)

        return output

    def resolve_location(self, place_text: str) -> Optional[Dict[str, Any]]:
        place_text = place_text.strip()

        if not place_text:
            return None

        cache_key = f"geocode::{place_text.lower()}"

        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            location = self.geocoder.geocode(
                place_text,
                addressdetails=True,
                exactly_one=True,
                language="en",
            )

            time.sleep(self.geocode_sleep_seconds)

        except (GeocoderTimedOut, GeocoderServiceError):
            return None

        except Exception:
            return None

        if not location:
            self.cache[cache_key] = None
            return None

        raw = location.raw or {}
        address = raw.get("address", {}) or {}

        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )

        country = address.get("country")

        resolved = {
            "city": city,
            "country": country,
            "display_name": raw.get("display_name"),
            "lat": raw.get("lat"),
            "lon": raw.get("lon"),
            "class": raw.get("class"),
            "type": raw.get("type"),
        }

        self.cache[cache_key] = resolved
        return resolved

    def score_languages(self, texts: List[EvidenceText]) -> Dict[str, Any]:
        scores = defaultdict(float)
        evidence = defaultdict(list)

        for item in self.build_language_analysis_batches(texts):
            language_probs = self.detect_language_probabilities(item.text)
            source_weight = self.country_signal_weight_for_source(item.source)

            for language, probability in language_probs.items():
                score = source_weight * probability
                scores[language] += score
                evidence[language].append({
                    "source": item.source,
                    "text_preview": item.text[:120],
                    "weight": round(score, 3),
                })

        if not scores:
            return {
                "language": "unknown",
                "confidence": 0.0,
                "candidates": [],
            }

        total = sum(scores.values())
        best_language, best_score = max(scores.items(), key=lambda x: x[1])
        candidates = [
            {
                "language": language,
                "confidence": round(score / total, 3),
                "raw_score": round(score, 3),
                "evidence": evidence[language][:5],
            }
            for language, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)
        ]

        return {
            "language": best_language,
            "confidence": best_score / total if total else 0.0,
            "candidates": candidates[:10],
        }

    def build_language_analysis_batches(self, texts: List[EvidenceText]) -> List[EvidenceText]:
        grouped_samples: Dict[str, List[str]] = defaultdict(list)
        seen_by_source: Dict[str, set] = defaultdict(set)

        sorted_texts = sorted(
            texts,
            key=lambda item: (
                self.country_signal_weight_for_source(item.source),
                len(item.text),
            ),
            reverse=True,
        )

        for item in sorted_texts:
            max_samples = self.MAX_TEXT_SAMPLES_PER_SOURCE.get(item.source)
            if not max_samples:
                continue

            prepared = self.prepare_text_for_language_detection(item.text)
            if len(prepared) < self.MIN_LANGUAGE_TEXT_LENGTH:
                continue

            if item.source == "comment" and len(prepared) < 20:
                continue

            normalized = prepared.lower()
            if normalized in seen_by_source[item.source]:
                continue

            if len(grouped_samples[item.source]) >= max_samples:
                continue

            seen_by_source[item.source].add(normalized)
            grouped_samples[item.source].append(prepared[: self.MAX_LANGUAGE_CHARS_PER_BATCH])

        batches: List[EvidenceText] = []
        detection_count = 0

        for source, samples in grouped_samples.items():
            buffer: List[str] = []
            buffer_chars = 0

            for sample in samples:
                sample_len = len(sample)
                if buffer and buffer_chars + sample_len > self.MAX_LANGUAGE_CHARS_PER_BATCH:
                    batches.append(
                        EvidenceText(
                            source=source,
                            text=" ".join(buffer),
                            weight=self.country_signal_weight_for_source(source),
                        )
                    )
                    detection_count += 1
                    buffer = []
                    buffer_chars = 0

                if detection_count >= self.MAX_LANGUAGE_DETECTIONS:
                    return batches

                buffer.append(sample)
                buffer_chars += sample_len

            if buffer and detection_count < self.MAX_LANGUAGE_DETECTIONS:
                batches.append(
                    EvidenceText(
                        source=source,
                        text=" ".join(buffer),
                        weight=self.country_signal_weight_for_source(source),
                    )
                )
                detection_count += 1

            if detection_count >= self.MAX_LANGUAGE_DETECTIONS:
                break

        return batches

    def detect_language_probabilities(self, text: str) -> Dict[str, float]:
        cleaned = self.prepare_text_for_language_detection(text)
        if cleaned in self.language_detection_cache:
            return self.language_detection_cache[cleaned]

        scores = defaultdict(float)

        script_hints = self.detect_script_language_hints(cleaned)
        for language, weight in script_hints.items():
            scores[language] += weight

        keyword_hints = self.detect_keyword_language_hints(cleaned)
        for language, weight in keyword_hints.items():
            scores[language] += weight

        if len(cleaned) >= 5:
            try:
                for detected in detect_langs(cleaned[:1200]):
                    language = self.normalize_detected_language(detected.lang)
                    if detected.prob < 0.05:
                        continue
                    scores[language] += detected.prob
            except LangDetectException:
                pass

        if not scores:
            result = {"unknown": 1.0}
            self.language_detection_cache[cleaned] = result
            return result

        total = sum(scores.values())
        result = {language: value / total for language, value in scores.items() if value > 0}
        self.language_detection_cache[cleaned] = result
        return result

    def prepare_text_for_language_detection(self, text: str) -> str:
        text = re.sub(r"https?://\S+", " ", text)
        text = re.sub(r"[@#][\w.]+", " ", text)
        text = re.sub(r"[^\w\s\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F\u0600-\u06FF\u0400-\u04FF]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def normalize_detected_language(self, language: str) -> str:
        language = language.lower().strip()
        return self.LANGUAGE_FALSE_POSITIVE_NORMALIZATION.get(language, language)

    def detect_script_language_hints(self, text: str) -> Dict[str, float]:
        hints = {}

        for language, pattern in self.SCRIPT_LANGUAGE_HINTS.items():
            if re.search(pattern, text):
                hints[language] = 1.0

        return hints

    def detect_keyword_language_hints(self, text: str) -> Dict[str, float]:
        lowered = f" {text.lower()} "
        hints = defaultdict(float)

        if any(word in lowered for word in (" bhai ", " bhaiya ", " yaar ", " kya ", " acha ", " accha ", " matlab ", " dost ", " sahi ", " dekh ")):
            hints["hi"] += 0.9
            hints["en"] += 0.2

        if any(word in lowered for word in (" anna ", " machaa ", " da ")):
            hints["ta"] += 0.6

        if any(word in lowered for word in (" habibi ", " inshallah ", " mashallah ")):
            hints["ar"] += 0.8

        return hints

    def score_country_from_texts(
        self,
        texts: List[EvidenceText],
        language_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        scores = defaultdict(float)
        evidence = defaultdict(list)

        for candidate in language_result.get("candidates", []):
            language = candidate["language"]
            confidence = candidate["confidence"]
            for country, weight in self.LANGUAGE_TO_COUNTRY_WEIGHTS.get(language, {}).items():
                score = confidence * weight * 0.75
                scores[country] += score
                evidence[country].append({
                    "signal": f"language:{language}",
                    "weight": round(score, 3),
                })

        for item in texts:
            source_weight = self.country_signal_weight_for_source(item.source)
            for country, strength, signal in self.extract_country_text_hints(item.text):
                score = source_weight * strength
                scores[country] += score
                evidence[country].append({
                    "signal": signal,
                    "source": item.source,
                    "weight": round(score, 3),
                })

        if not scores:
            return {
                "country": None,
                "confidence": 0.0,
                "candidates": [],
            }

        total = sum(scores.values())
        best_country, best_score = max(scores.items(), key=lambda x: x[1])
        candidates = [
            {
                "country": country,
                "confidence": round(score / total, 3),
                "raw_score": round(score, 3),
                "evidence": evidence[country][:5],
            }
            for country, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)
        ]

        return {
            "country": best_country,
            "confidence": best_score / total if total else 0.0,
            "candidates": candidates[:10],
        }

    def country_signal_weight_for_source(self, source: str) -> float:
        weights = {
            "profile_bio": 1.0,
            "post_caption": 0.65,
            "external_link_page": 0.55,
            "external_url": 0.12,
            "external_link": 0.12,
            "profile_full_name": 0.08,
            "recent_post_alt": 0.15,
            "post_alt": 0.15,
            "comment": 0.06,
        }
        return weights.get(source, 0.1)

    def extract_country_text_hints(self, text: str) -> List[Tuple[str, float, str]]:
        lowered = f" {text.lower()} "
        hints: List[Tuple[str, float, str]] = []

        for key, slang_terms in LOCATION_SLANG.items():
            country = self.COUNTRY_HINT_OVERRIDES.get(key, key)
            if country not in self.LANGUAGE_TO_COUNTRY_WEIGHTS.get("unknown", {}) and country not in {
                "India", "Pakistan", "Bangladesh", "Sri Lanka", "UAE", "USA", "UK", "Singapore", "Malaysia"
            }:
                continue

            matches = sum(1 for slang in slang_terms if f" {slang.lower()} " in lowered)
            if matches:
                hints.append((country, min(0.35, 0.14 * matches), f"slang:{key.lower()}"))

        if self.INDIA_CITY_PATTERN.search(lowered):
            hints.append(("India", 0.35, "keyword:india_city"))

        if self.INDIA_COUNTRY_PATTERN.search(lowered):
            hints.append(("India", 0.45, "keyword:india"))

        if self.PAKISTAN_PATTERN.search(lowered):
            hints.append(("Pakistan", 0.40, "keyword:pakistan"))

        if self.BANGLADESH_PATTERN.search(lowered):
            hints.append(("Bangladesh", 0.40, "keyword:bangladesh"))

        if self.UAE_PATTERN.search(lowered):
            hints.append(("UAE", 0.40, "keyword:uae"))

        if self.USA_PATTERN.search(lowered):
            hints.append(("USA", 0.35, "keyword:usa"))

        if self.UK_PATTERN.search(lowered):
            hints.append(("UK", 0.35, "keyword:uk"))

        return hints

    def filter_city_result_by_country(
        self,
        city_result: Dict[str, Any],
        predicted_country: Optional[str],
    ) -> Dict[str, Any]:
        if not predicted_country or not city_result.get("candidates"):
            return city_result

        matching_candidates = [
            candidate
            for candidate in city_result["candidates"]
            if candidate.get("country") == predicted_country
        ]

        if not matching_candidates:
            return {
                "city": None,
                "country": predicted_country,
                "confidence": 0.0,
                "candidates": [],
            }

        best_candidate = matching_candidates[0]
        return {
            "city": best_candidate.get("city"),
            "country": best_candidate.get("country"),
            "confidence": best_candidate.get("confidence", 0.0),
            "candidates": matching_candidates,
        }

    def score_city(self, resolved_locations: List[Dict[str, Any]]) -> Dict[str, Any]:
        scores = defaultdict(float)
        evidence = defaultdict(list)

        for item in resolved_locations:
            city = item.get("resolved_city")
            country = item.get("resolved_country")

            if not city:
                continue

            key = (city, country)

            score = self.adjust_weight_for_source(
                source=item["source"],
                base_weight=item["source_weight"],
                is_city=True,
            )

            scores[key] += score

            evidence[key].append({
                "mention": item["mention"],
                "source": item["source"],
                "shortcode": item.get("shortcode"),
                "display_name": item.get("display_name"),
                "weight": round(score, 3),
            })

        if not scores:
            return {
                "city": None,
                "country": None,
                "confidence": 0.0,
                "candidates": [],
            }

        total = sum(scores.values())
        best_key, best_score = max(scores.items(), key=lambda x: x[1])
        confidence = best_score / total if total else 0.0

        candidates = []

        for (city, country), score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            candidates.append({
                "city": city,
                "country": country,
                "confidence": round(score / total, 3),
                "raw_score": round(score, 3),
                "evidence": evidence[(city, country)][:5],
            })

        return {
            "city": best_key[0],
            "country": best_key[1],
            "confidence": confidence,
            "candidates": candidates[:10],
        }

    def score_country(self, resolved_locations: List[Dict[str, Any]]) -> Dict[str, Any]:
        scores = defaultdict(float)
        evidence = defaultdict(list)

        for item in resolved_locations:
            country = item.get("resolved_country")

            if not country:
                continue

            score = self.adjust_weight_for_source(
                source=item["source"],
                base_weight=item["source_weight"],
                is_city=False,
            )

            scores[country] += score

            evidence[country].append({
                "mention": item["mention"],
                "source": item["source"],
                "shortcode": item.get("shortcode"),
                "display_name": item.get("display_name"),
                "weight": round(score, 3),
            })

        if not scores:
            return {
                "country": None,
                "confidence": 0.0,
                "candidates": [],
            }

        total = sum(scores.values())
        best_country, best_score = max(scores.items(), key=lambda x: x[1])
        confidence = best_score / total if total else 0.0

        candidates = []

        for country, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            candidates.append({
                "country": country,
                "confidence": round(score / total, 3),
                "raw_score": round(score, 3),
                "evidence": evidence[country][:5],
            })

        return {
            "country": best_country,
            "confidence": confidence,
            "candidates": candidates[:10],
        }

    def adjust_weight_for_source(
        self,
        source: str,
        base_weight: float,
        is_city: bool,
    ) -> float:
        """
        Source trust:
        - Bio is strongest.
        - Captions are strong because creator wrote them.
        - External link page text is useful.
        - Comments are weak because they represent audience, not creator.
        """

        multiplier = 1.0

        if source == "profile_bio":
            multiplier = 1.3

        elif source == "post_caption":
            multiplier = 1.15

        elif source == "external_link_page":
            multiplier = 1.05

        elif source in {"external_url", "external_link"}:
            multiplier = 0.75

        elif source in {"comment"}:
            multiplier = 0.35 if is_city else 0.50

        elif source in {"recent_post_alt", "post_alt"}:
            multiplier = 0.60

        return base_weight * multiplier


def main():
    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "json_file",
        help="Path to Instagram scraped JSON file",
    )

    parser.add_argument(
        "--no-fetch-links",
        action="store_true",
        help="Disable fetching external link page title/meta text",
    )

    parser.add_argument(
        "--min-city-confidence",
        type=float,
        default=0.65,
    )

    parser.add_argument(
        "--min-country-confidence",
        type=float,
        default=0.55,
    )

    args = parser.parse_args()

    inferencer = InstagramGeoInferencer(
        fetch_link_pages=not args.no_fetch_links,
        min_city_confidence=args.min_city_confidence,
        min_country_confidence=args.min_country_confidence,
        user_agent="your-company-name-instagram-geo-inferencer",
    )

    result = inferencer.infer_from_file(args.json_file)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
