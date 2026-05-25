"""
Content niche analysis for demographics responses.

The analyzer scores niches from actual scrape evidence: profile category/bio,
post captions, hashtags, comments, and post types. It intentionally avoids
fixed demographic percentages; labels are returned only when matching evidence
exists in the scraped data.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List


HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
WORD_RE = re.compile(r"[A-Za-z\u0900-\u097F]{2,}")


NICHE_TAXONOMY: Dict[str, Dict[str, Iterable[str]]] = {
    "couple_family_comedy": {
        "label": "Couple and family comedy",
        "terms": (
            "couple",
            "couples",
            "family",
            "husband",
            "wife",
            "marriage",
            "jeth",
            "devar",
            "bhabhi",
            "mayka",
            "mother",
            "daughter",
            "girlfriend",
            "boyfriend",
            "relatable",
            "funny",
            "comedy",
        ),
        "brand_fit": (
            "family products",
            "food and snacks",
            "household products",
            "regional entertainment campaigns",
        ),
    },
    "fashion_ethnic_wear": {
        "label": "Fashion and ethnic wear",
        "terms": (
            "fashion",
            "saree",
            "sari",
            "ethnic",
            "outfit",
            "dress",
            "style",
            "photoshoot",
            "navratricollection",
        ),
        "brand_fit": (
            "ethnic wear",
            "sarees",
            "jewellery",
            "fashion marketplaces",
        ),
    },
    "festival_culture": {
        "label": "Festival and culture",
        "terms": (
            "navratri",
            "garba",
            "diwali",
            "ganpati",
            "ganapati",
            "festival",
            "traditional",
            "indian",
        ),
        "brand_fit": (
            "festival campaigns",
            "regional brands",
            "ethnic wear",
            "event promotions",
        ),
    },
    "devotional_spiritual": {
        "label": "Devotional and spiritual",
        "terms": (
            "khatushyam",
            "shyam",
            "hanuman",
            "balaji",
            "jai",
            "shree",
            "ram",
            "pray",
            "blessed",
            "devotional",
            "temple",
        ),
        "brand_fit": (
            "devotional content",
            "spiritual travel",
            "festival campaigns",
        ),
    },
    "beauty_skincare": {
        "label": "Beauty and skincare",
        "terms": (
            "beauty",
            "skincare",
            "skin",
            "facewash",
            "serum",
            "pimple",
            "himalaya",
            "nivea",
            "glow",
        ),
        "brand_fit": (
            "skincare",
            "beauty",
            "personal care",
            "cosmetics",
        ),
    },
    "student_youth_life": {
        "label": "Student and youth life",
        "terms": (
            "student",
            "exam",
            "result",
            "marks",
            "pg",
            "girls",
            "boys",
            "friend",
            "friends",
            "college",
            "school",
        ),
        "brand_fit": (
            "education apps",
            "youth brands",
            "snacks",
            "mobile apps",
        ),
    },
    "sports_trending": {
        "label": "Sports and trending topics",
        "terms": (
            "ipl",
            "cricket",
            "premierleague",
            "premier",
            "league",
            "arsenal",
            "champions",
            "football",
            "sports",
        ),
        "brand_fit": (
            "sports campaigns",
            "trending entertainment",
            "fan engagement campaigns",
        ),
    },
    "travel_lifestyle": {
        "label": "Travel and lifestyle",
        "terms": (
            "travel",
            "trip",
            "dubai",
            "maldives",
            "abu dhabi",
            "vacation",
            "vlog",
            "vlogs",
            "lifestyle",
        ),
        "brand_fit": (
            "travel",
            "hospitality",
            "lifestyle products",
        ),
    },
}


def _safe_text(value: Any) -> str:
    return "" if value is None else str(value)


def _extract_hashtags(text: str) -> List[str]:
    return [tag.lower() for tag in HASHTAG_RE.findall(text or "")]


def _extract_words(text: str) -> List[str]:
    return [word.lower() for word in WORD_RE.findall(text or "")]


class NicheAnalyzer:
    """Score creator/content niches from scraped public text."""

    SOURCE_WEIGHTS = {
        "profile_category": 2.0,
        "profile_bio": 1.2,
        "post_caption": 1.0,
        "hashtag": 1.4,
        "comment": 0.15,
    }

    MAX_COMMENT_SAMPLES = 250

    def analyze(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
        comments: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        scores: Counter = Counter()
        evidence: Dict[str, Counter] = defaultdict(Counter)
        hashtag_counter: Counter = Counter()
        word_counter: Counter = Counter()
        content_type_counter: Counter = Counter()

        def score_text(text: Any, source: str) -> None:
            value = _safe_text(text).lower()
            if not value:
                return

            words = _extract_words(value)
            word_counter.update(words)

            for niche_key, config in NICHE_TAXONOMY.items():
                for term in config["terms"]:
                    term_value = str(term).lower()
                    if " " in term_value:
                        matched = term_value in value
                    else:
                        matched = re.search(rf"\b{re.escape(term_value)}\b", value) is not None
                    if matched:
                        weight = self.SOURCE_WEIGHTS.get(source, 0.3)
                        scores[niche_key] += weight
                        evidence[niche_key][term_value] += 1

        score_text(profile.get("category"), "profile_category")
        score_text(profile.get("biography") or profile.get("bio"), "profile_bio")

        for post in posts:
            post_type = _safe_text(post.get("post_type") or post.get("media_type")).strip().lower()
            if post_type:
                content_type_counter[post_type] += 1

            caption = post.get("caption") or post.get("description") or post.get("alt") or ""
            score_text(caption, "post_caption")

            hashtags = list(post.get("hashtags") or [])
            hashtags.extend(_extract_hashtags(_safe_text(caption)))
            hashtag_counter.update(tag.lower().strip("#") for tag in hashtags if tag)
            for hashtag in hashtags:
                score_text(str(hashtag).replace("_", " "), "hashtag")

        for comment in comments[: self.MAX_COMMENT_SAMPLES]:
            score_text(comment.get("text"), "comment")

        total_score = sum(scores.values())
        ranked = []
        for key, score in scores.most_common():
            config = NICHE_TAXONOMY[key]
            percentage = round((score / total_score) * 100, 1) if total_score > 0 else 0.0
            ranked.append(
                {
                    "key": key,
                    "label": config["label"],
                    "score": round(score, 2),
                    "percentage": percentage,
                    "evidence": [
                        {"term": term, "count": count}
                        for term, count in evidence[key].most_common(5)
                    ],
                }
            )

        top_hashtags = [
            {"tag": tag, "count": count}
            for tag, count in hashtag_counter.most_common(15)
        ]
        top_keywords = [
            {"term": term, "count": count}
            for term, count in word_counter.most_common(20)
            if len(term) > 2
        ][:15]

        brand_fit = []
        for item in ranked[:3]:
            for fit in NICHE_TAXONOMY[item["key"]].get("brand_fit", ()):
                if fit not in brand_fit:
                    brand_fit.append(fit)

        confidence = "low"
        if total_score >= 10 and len(posts) >= 3:
            confidence = "high"
        elif total_score >= 4:
            confidence = "medium"

        return {
            "primary": ranked[0]["label"] if ranked else None,
            "secondary": [item["label"] for item in ranked[1:5]],
            "distribution": ranked[:6],
            "topHashtags": top_hashtags,
            "topKeywords": top_keywords,
            "contentTypes": [
                {"type": post_type, "count": count}
                for post_type, count in content_type_counter.most_common()
            ],
            "brandFit": brand_fit[:10],
            "confidence": confidence,
            "evidencePosts": len(posts),
            "evidenceComments": min(len(comments), self.MAX_COMMENT_SAMPLES),
        }
