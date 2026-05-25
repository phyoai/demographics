"""
Layer 2: Feature Extraction
Extract features from comments, posts, and profile data
"""
import re
import emoji
from collections import Counter
from datetime import datetime
from langdetect import DetectorFactory, LangDetectException, detect, detect_langs

try:
    from nameparser import HumanName
except Exception:  # pragma: no cover - optional dependency
    HumanName = None

try:
    import pycountry
except Exception:  # pragma: no cover - optional dependency
    pycountry = None

try:
    from ..config import (
        AGE_HASHTAGS,
        LOCATION_SLANG,
        SPAM_PATTERNS,
        MALE_EMOJIS,
        FEMALE_EMOJIS,
        MALE_KEYWORDS,
        FEMALE_KEYWORDS,
    )
except ImportError:
    from config import AGE_HASHTAGS, LOCATION_SLANG, SPAM_PATTERNS, MALE_EMOJIS, FEMALE_EMOJIS, MALE_KEYWORDS, FEMALE_KEYWORDS


DetectorFactory.seed = 0

NON_NAME_TOKENS = {
    "the",
    "its",
    "mr",
    "mrs",
    "ms",
    "dr",
    "official",
    "real",
    "code",
    "bit",
    "tech",
    "dev",
    "fan",
    "club",
    "team",
    "edit",
    "edits",
    "status",
    "reels",
    "vlogs",
    "page",
}

CITY_ALIASES = {
    "Delhi": ["delhi", "new delhi", "dilli", "ncr"],
    "Noida": ["noida", "greater noida"],
    "Gurugram": ["gurugram", "gurgaon", "ggn"],
    "Ghaziabad": ["ghaziabad", "gzb"],
    "Faridabad": ["faridabad", "fbd"],
    "Mumbai": ["mumbai", "bombay"],
    "Bangalore": ["bangalore", "bengaluru", "blr"],
    "Hyderabad": ["hyderabad", "hyd"],
    "Chennai": ["chennai", "madras"],
    "Pune": ["pune"],
    "Kolkata": ["kolkata", "calcutta"],
    "Ahmedabad": ["ahmedabad"],
    "Vadodara": ["vadodara", "baroda"],
    "Rajkot": ["rajkot"],
    "Jaipur": ["jaipur"],
    "Lucknow": ["lucknow"],
    "Surat": ["surat"],
    "Indore": ["indore"],
    "Jabalpur": ["jabalpur"],
    "Narsinghpur": ["narsinghpur", "narsimhapur"],
    "Patna": ["patna"],
    "Cuttack": ["cuttack"],
    "Bargarh": ["bargarh", "baragarh"],
    "Bhubaneswar": ["bhubaneswar", "odisha", "orissa"],
    "Khairagarh": ["khairagarh"],
    "Raipur": ["raipur", "chhattisgarh"],
    "Bhopal": ["bhopal", "madhya pradesh", "mp"],
    "Nagpur": ["nagpur"],
    "Kanpur": ["kanpur"],
    "Ludhiana": ["ludhiana"],
    "Chandigarh": ["chandigarh"],
    "Kochi": ["kochi", "cochin"],
    "Thiruvananthapuram": ["thiruvananthapuram", "trivandrum"],
    "Kozhikode": ["kozhikode", "calicut"],
    "Mysore": ["mysore", "mysuru"],
    "Mangalore": ["mangalore", "mangaluru"],
    "Coimbatore": ["coimbatore"],
    "Madurai": ["madurai"],
    "Visakhapatnam": ["visakhapatnam", "vizag"],
    "Vijayawada": ["vijayawada"],
    "Nashik": ["nashik", "nasik"],
    "Karachi": ["karachi"],
    "Lahore": ["lahore"],
    "Islamabad": ["islamabad"],
    "Dhaka": ["dhaka"],
    "Dubai": ["dubai"],
    "Abu Dhabi": ["abu dhabi"],
    "London": ["london"],
    "New York": ["new york", "nyc"],
    "Los Angeles": ["los angeles", "la"],
    "Singapore": ["singapore"],
}

CITY_TO_COUNTRY = {
    "Delhi": "India",
    "Noida": "India",
    "Gurugram": "India",
    "Ghaziabad": "India",
    "Faridabad": "India",
    "Mumbai": "India",
    "Bangalore": "India",
    "Hyderabad": "India",
    "Chennai": "India",
    "Pune": "India",
    "Kolkata": "India",
    "Ahmedabad": "India",
    "Vadodara": "India",
    "Rajkot": "India",
    "Jaipur": "India",
    "Lucknow": "India",
    "Surat": "India",
    "Indore": "India",
    "Jabalpur": "India",
    "Narsinghpur": "India",
    "Patna": "India",
    "Cuttack": "India",
    "Bargarh": "India",
    "Bhubaneswar": "India",
    "Khairagarh": "India",
    "Raipur": "India",
    "Bhopal": "India",
    "Nagpur": "India",
    "Kanpur": "India",
    "Ludhiana": "India",
    "Chandigarh": "India",
    "Kochi": "India",
    "Thiruvananthapuram": "India",
    "Kozhikode": "India",
    "Mysore": "India",
    "Mangalore": "India",
    "Coimbatore": "India",
    "Madurai": "India",
    "Visakhapatnam": "India",
    "Vijayawada": "India",
    "Nashik": "India",
    "Karachi": "Pakistan",
    "Lahore": "Pakistan",
    "Islamabad": "Pakistan",
    "Dhaka": "Bangladesh",
    "Dubai": "UAE",
    "Abu Dhabi": "UAE",
    "London": "UK",
    "New York": "USA",
    "Los Angeles": "USA",
    "Singapore": "Singapore",
}

INDIAN_STATE_ALIASES = {
    "Andhra Pradesh": ["andhra pradesh"],
    "Assam": ["assam"],
    "Bihar": ["bihar"],
    "Chandigarh": ["chandigarh"],
    "Chhattisgarh": ["chhattisgarh", "chattisgarh"],
    "Delhi": ["delhi", "delhi ncr", "new delhi"],
    "Goa": ["goa"],
    "Gujarat": ["gujarat"],
    "Haryana": ["haryana"],
    "Himachal Pradesh": ["himachal pradesh", "himachal"],
    "Jharkhand": ["jharkhand"],
    "Karnataka": ["karnataka"],
    "Kerala": ["kerala"],
    "Madhya Pradesh": ["madhya pradesh", "mp"],
    "Maharashtra": ["maharashtra"],
    "Odisha": ["odisha", "orissa"],
    "Punjab": ["punjab"],
    "Rajasthan": ["rajasthan"],
    "Tamil Nadu": ["tamil nadu"],
    "Telangana": ["telangana"],
    "Uttar Pradesh": ["uttar pradesh"],
    "Uttarakhand": ["uttarakhand", "uttrakhand"],
    "West Bengal": ["west bengal"],
}

# Selected high-signal Indian PIN prefixes. This is intentionally not a full
# postal database; it only boosts locations when the prefix is distinctive.
INDIAN_PIN_PREFIX_TO_LOCATION = {
    "110": ("Delhi", "Delhi"),
    "201": ("Noida", "Uttar Pradesh"),
    "122": ("Gurugram", "Haryana"),
    "400": ("Mumbai", "Maharashtra"),
    "411": ("Pune", "Maharashtra"),
    "422": ("Nashik", "Maharashtra"),
    "440": ("Nagpur", "Maharashtra"),
    "560": ("Bangalore", "Karnataka"),
    "500": ("Hyderabad", "Telangana"),
    "600": ("Chennai", "Tamil Nadu"),
    "700": ("Kolkata", "West Bengal"),
    "380": ("Ahmedabad", "Gujarat"),
    "390": ("Vadodara", "Gujarat"),
    "395": ("Surat", "Gujarat"),
    "394": ("Surat", "Gujarat"),
    "360": ("Rajkot", "Gujarat"),
    "302": ("Jaipur", "Rajasthan"),
    "226": ("Lucknow", "Uttar Pradesh"),
    "208": ("Kanpur", "Uttar Pradesh"),
    "800": ("Patna", "Bihar"),
    "751": ("Bhubaneswar", "Odisha"),
    "768": ("Bargarh", "Odisha"),
    "487": ("Narsinghpur", "Madhya Pradesh"),
    "491": ("Khairagarh", "Chhattisgarh"),
    "462": ("Bhopal", "Madhya Pradesh"),
    "452": ("Indore", "Madhya Pradesh"),
    "141": ("Ludhiana", "Punjab"),
    "160": ("Chandigarh", "Chandigarh"),
    "682": ("Kochi", "Kerala"),
    "695": ("Thiruvananthapuram", "Kerala"),
    "673": ("Kozhikode", "Kerala"),
    "570": ("Mysore", "Karnataka"),
    "575": ("Mangalore", "Karnataka"),
    "641": ("Coimbatore", "Tamil Nadu"),
    "625": ("Madurai", "Tamil Nadu"),
    "530": ("Visakhapatnam", "Andhra Pradesh"),
    "520": ("Vijayawada", "Andhra Pradesh"),
}

INDIAN_PIN_CONTEXT_WORDS = {
    "from",
    "in",
    "city",
    "district",
    "jila",
    "zilla",
    "village",
    "area",
    "address",
    "pin",
    "pincode",
    "postal",
    "se",
}

COUNTRY_ALIASES = {
    "India": ["india", "bharat", "hindustan", "indian"],
    "USA": ["usa", "us", "america", "united states"],
    "UK": ["uk", "united kingdom", "britain", "england"],
    "UAE": ["uae", "dubai", "abu dhabi", "emirates"],
    "Pakistan": ["pakistan", "pakistani"],
    "Bangladesh": ["bangladesh", "bangladeshi"],
    "Canada": ["canada", "canadian"],
    "Australia": ["australia", "aussie"],
    "Singapore": ["singapore"],
}

COUNTRY_CANONICAL_OVERRIDES = {
    "united states": "USA",
    "united states of america": "USA",
    "united kingdom": "UK",
    "united kingdom of great britain and northern ireland": "UK",
    "united arab emirates": "UAE",
}

COUNTRY_ALIAS_SKIP_WORDS = {
    "and",
    "of",
    "the",
    "republic",
    "state",
    "states",
    "island",
    "islands",
}


def _canonical_country_name(value):
    if not value:
        return None

    candidate = str(value).strip()
    if not candidate:
        return None

    candidate_lower = candidate.lower()
    override = COUNTRY_CANONICAL_OVERRIDES.get(candidate_lower)
    if override:
        return override

    for country, aliases in COUNTRY_ALIASES.items():
        if candidate_lower == country.lower() or candidate_lower in aliases:
            return country

    if pycountry is not None:
        try:
            country = pycountry.countries.lookup(candidate)
            normalized = COUNTRY_CANONICAL_OVERRIDES.get(country.name.lower())
            return normalized or country.name
        except LookupError:
            pass

    return candidate


def _build_reference_country_aliases():
    aliases_by_country = {}

    if pycountry is None:
        return aliases_by_country

    for country in pycountry.countries:
        canonical = _canonical_country_name(country.name)
        if not canonical:
            continue

        aliases = aliases_by_country.setdefault(canonical, set())
        for attr in ("name", "official_name", "common_name"):
            value = getattr(country, attr, None)
            if not value:
                continue
            normalized = str(value).strip().lower()
            if len(normalized) <= 3 or normalized in COUNTRY_ALIAS_SKIP_WORDS:
                continue
            aliases.add(normalized)

    return {
        country: sorted(aliases, key=len, reverse=True)
        for country, aliases in aliases_by_country.items()
        if aliases
    }


REFERENCE_COUNTRY_ALIASES = _build_reference_country_aliases()
REFERENCE_COUNTRY_ALIAS_LOOKUP = {
    alias: country
    for country, aliases in REFERENCE_COUNTRY_ALIASES.items()
    for alias in aliases
}
REFERENCE_COUNTRY_MAX_ALIAS_WORDS = max(
    (len(alias.split()) for alias in REFERENCE_COUNTRY_ALIAS_LOOKUP),
    default=1,
)

HINGLISH_WORDS = {
    "bhai",
    "yaar",
    "kya",
    "acha",
    "accha",
    "matlab",
    "dost",
    "dekh",
    "sahi",
    "nahi",
    "hai",
    "hoon",
    "aap",
    "mujhe",
    "maine",
    "kaise",
    "karo",
    "bada",
    "bahut",
    "ji",
    "sir",
    "sar",
}

LANGUAGE_NORMALIZATION = {
    "hindi": "hi",
    "tamil": "ta",
    "telugu": "te",
    "kannada": "kn",
    "bengali": "bn",
    "marathi": "mr",
}


class FeatureExtractor:
    """Extract features from Instagram data"""
    
    def __init__(self):
        self._language_details_cache = {}

    @staticmethod
    def clean_text(text):
        if not text:
            return ""

        cleaned = str(text)
        cleaned = re.sub(r"https?://\S+", " ", cleaned)
        cleaned = re.sub(r"@\w+", " ", cleaned)
        cleaned = re.sub(r"#", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _contains_phrase(text_lower, phrase):
        escaped = re.escape(phrase.lower())
        if " " in phrase:
            return re.search(rf"(?<!\w){escaped}(?!\w)", text_lower) is not None
        return re.search(rf"\b{escaped}\b", text_lower) is not None

    def parse_name_parts(self, full_name, username):
        raw_name = self.clean_text(full_name)

        if raw_name and HumanName is not None:
            parsed = HumanName(raw_name)
            first = parsed.first.strip().lower()
            last = parsed.last.strip().lower()
            if first and first not in NON_NAME_TOKENS:
                return {
                    "first_name": first,
                    "last_name": last or None,
                    "source": "full_name",
                    "confidence": 0.95,
                }

        if raw_name:
            ascii_name = re.sub(r"[^A-Za-z\s]", " ", raw_name)
            parts = [
                part.lower()
                for part in ascii_name.split()
                if len(part) > 1 and part.lower() not in NON_NAME_TOKENS
            ]
            if parts:
                return {
                    "first_name": parts[0],
                    "last_name": parts[-1] if len(parts) > 1 else None,
                    "source": "full_name",
                    "confidence": 0.9,
                }

        username_name = self.extract_first_name(username)
        return {
            "first_name": username_name,
            "last_name": None,
            "source": "username" if username_name else "none",
            "confidence": 0.55 if username_name else 0.0,
        }
    
    def extract_first_name(self, username):
        """
        Extract first name from username - IMPROVED
        Examples: sam.singh.07 -> sam, vika_s17024 -> vika, priya_sharma -> priya
        codebitabhi -> abhi, abhishek_sharma -> abhishek
        """
        if not username:
            return None
        
        username_lower = username.lower()
        
        # Special patterns: extract name from compounds like "codebitabhi" -> "abhi"
        # Common Indian name extraction patterns
        indian_names = {
            'abhi': ['abhishek', 'abhinav', 'abhimanyu'],
            'raj': ['rajesh', 'rajat', 'rajeev', 'rajan'],
            'amit': ['amit'],
            'priya': ['priya'],
            'neha': ['neha'],
            'rohit': ['rohit'],
            'rahul': ['rahul'],
            'ankit': ['ankit'],
            'nikita': ['nikita'],
            'divya': ['divya']
        }
        
        # Check for compound usernames
        for short_name, full_names in indian_names.items():
            if short_name in username_lower:
                return short_name
        
        # Remove numbers and special characters, split into parts
        name = re.sub(r'[0-9]', '', username_lower)
        name = re.sub(r'[_\-\.]', ' ', name)
        parts = [p for p in name.strip().split() if len(p) > 1]
        
        if parts:
            # Return the first meaningful part
            first_part = parts[0]
            # Filter out common prefixes/suffixes
            if first_part not in NON_NAME_TOKENS:
                return first_part
            elif len(parts) > 1:
                return parts[1]
        
        # Last resort: try to extract any known Indian name from username
        all_indian_names = ['abhi', 'abhishek', 'raj', 'rajesh', 'amit', 'rohit', 'rahul', 'ankit', 
                           'priya', 'neha', 'pooja', 'anjali', 'divya', 'ravi', 'anil', 'sunil',
                           'vijay', 'ajay', 'sanjay', 'rohan', 'arjun', 'karan', 'varun']
        for name in all_indian_names:
            if name in username_lower:
                return name
        
        return None
    
    def extract_emojis(self, text):
        """Extract all emojis from text"""
        if not text:
            return []
        return [c for c in text if c in emoji.EMOJI_DATA]
    
    def calculate_emoji_density(self, text):
        """Calculate emoji density (ratio of emojis to total characters)"""
        if not text:
            return 0.0
        
        emojis = self.extract_emojis(text)
        return len(emojis) / len(text) if len(text) > 0 else 0.0
    
    def detect_language_details(self, text):
        """Return deterministic language label plus confidence and alternatives."""
        if not text or len(text) < 3:
            return {"language": "unknown", "confidence": 0.0, "candidates": []}

        text_clean = self.clean_text(text)
        text_clean = re.sub(r"[^\w\s]", " ", text_clean)
        text_clean = "".join(c for c in text_clean if not emoji.is_emoji(c))
        text_clean = re.sub(r"\s+", " ", text_clean).strip()

        if len(text_clean) < 8:
            return {"language": "unknown", "confidence": 0.0, "candidates": []}

        cache_key = text_clean.lower()[:500]
        language_cache = getattr(self, "_language_details_cache", None)
        if isinstance(language_cache, dict) and cache_key in language_cache:
            return language_cache[cache_key]

        text_lower = text_clean.lower()
        hinglish_hits = sum(1 for word in HINGLISH_WORDS if self._contains_phrase(text_lower, word))
        if hinglish_hits >= 2:
            result = {
                "language": "hi",
                "confidence": min(0.95, 0.55 + hinglish_hits * 0.08),
                "candidates": [{"language": "hi", "confidence": min(0.95, 0.55 + hinglish_hits * 0.08)}],
            }
            if isinstance(language_cache, dict):
                language_cache[cache_key] = result
            return result

        script_language_hints = [
            (r"[\u0900-\u097F]", "hi"),
            (r"[\u0A80-\u0AFF]", "gu"),
            (r"[\u0980-\u09FF]", "bn"),
            (r"[\u0B80-\u0BFF]", "ta"),
            (r"[\u0C00-\u0C7F]", "te"),
            (r"[\u0C80-\u0CFF]", "kn"),
            (r"[\u0D00-\u0D7F]", "ml"),
        ]
        for pattern, language in script_language_hints:
            if re.search(pattern, text_clean):
                result = {
                    "language": language,
                    "confidence": 0.78,
                    "candidates": [{"language": language, "confidence": 0.78}],
                }
                if isinstance(language_cache, dict):
                    language_cache[cache_key] = result
                return result

        ascii_words = re.findall(r"[a-z]+", text_lower)
        is_ascii_text = all(ord(char) < 128 for char in text_clean)
        common_reaction_words = {
            "amazing",
            "awesome",
            "beautiful",
            "best",
            "cute",
            "friend",
            "funny",
            "good",
            "great",
            "haha",
            "hahaha",
            "hilarious",
            "lol",
            "love",
            "nice",
            "ok",
            "post",
            "same",
            "super",
            "true",
            "video",
            "wow",
            "yes",
        }
        if (
            is_ascii_text
            and len(text_clean) < 32
            and ascii_words
            and all(word in common_reaction_words or word.isdigit() for word in ascii_words)
        ):
            result = {
                "language": "en",
                "confidence": 0.45,
                "candidates": [{"language": "en", "confidence": 0.45}],
            }
            if isinstance(language_cache, dict):
                language_cache[cache_key] = result
            return result

        try:
            detected_candidates = detect_langs(text_clean)
        except LangDetectException:
            return {"language": "unknown", "confidence": 0.0, "candidates": []}

        candidates = [
            {"language": candidate.lang, "confidence": round(float(candidate.prob), 3)}
            for candidate in detected_candidates[:3]
        ]
        if not candidates:
            return {"language": "unknown", "confidence": 0.0, "candidates": []}

        detected = candidates[0]["language"]
        confidence = candidates[0]["confidence"]
        common_short_text_false_positives = {"so", "vi", "pl", "fi", "nl", "da", "no", "sv", "sq", "ca", "id", "tl", "cy"}
        if detected in common_short_text_false_positives and len(text_clean) < 24 and confidence < 0.92:
            result = {
                "language": "en",
                "confidence": 0.45,
                "candidates": candidates,
            }
            if isinstance(language_cache, dict):
                language_cache[cache_key] = result
            return result

        result = {"language": detected, "confidence": confidence, "candidates": candidates}
        if isinstance(language_cache, dict):
            language_cache[cache_key] = result
        return result

    def detect_language(self, text):
        """Detect language of text."""
        return self.detect_language_details(text)["language"]
    
    def detect_user_language(self, username, comment_text):
        """
        Detect user's actual language based on username + comment
        For Indian users, detect if they speak Hindi/regional languages even if commenting in English
        """
        # Indian name patterns indicate Hindi/regional language speakers
        indian_name_patterns = {
            'hi': ['singh', 'kumar', 'sharma', 'gupta', 'yadav', 'verma', 'jain', 'agarwal', 
                     'raj', 'ravi', 'amit', 'ankit', 'rohit', 'rahul', 'deepak', 'sanjay', 'vijay',
                     'priya', 'neha', 'pooja', 'anjali', 'kavya', 'divya', 'arora', 'kapoor',
                     'malhotra', 'bhatia', 'sethi', 'saxena', 'mittal', 'abhi', 'abhishek',
                     'saini', 'tyagi', 'chauhan', 'pandit', 'joshi', 'negi'],
            'ta': ['raman', 'krishnan', 'murugan', 'sundaram', 'rajesh', 'iyer', 'venkat', 'swamy'],
            'te': ['reddy', 'rao', 'naidu', 'prasad', 'chowdary'],
            'kn': ['gowda', 'hegde', 'shetty', 'nayak'],
            'bn': ['das', 'sen', 'chatterjee', 'banerjee', 'ghosh', 'bose', 'roy', 'dutta'],
            'mr': ['patil', 'kulkarni', 'deshmukh', 'pawar', 'shinde', 'jadhav']
        }
        
        if username:
            username_lower = username.lower()
            
            # Check for Indian name patterns
            for lang, patterns in indian_name_patterns.items():
                if any(pattern in username_lower for pattern in patterns):
                    return lang
        
        # Check comment for Hindi/Indian language words
        if comment_text:
            text_lower = comment_text.lower()
            if sum(1 for word in HINGLISH_WORDS if self._contains_phrase(text_lower, word)) >= 2:
                return 'hi'

        # Fallback to regular detection
        detected = self.detect_language_details(comment_text) if comment_text else {"language": "unknown"}
        language = detected.get("language", "unknown")
        return LANGUAGE_NORMALIZATION.get(language, language)

    def _has_postal_location_context(self, text_lower):
        if any(self._contains_phrase(text_lower, word) for word in INDIAN_PIN_CONTEXT_WORDS):
            return True

        for aliases in CITY_ALIASES.values():
            if any(self._contains_phrase(text_lower, alias) for alias in aliases):
                return True

        for aliases in INDIAN_STATE_ALIASES.values():
            if any(self._contains_phrase(text_lower, alias) for alias in aliases):
                return True

        return False

    def extract_postal_location_mentions(self, text):
        text_lower = self.clean_text(text).lower()
        city_counts = Counter()
        state_counts = Counter()
        country_counts = Counter()
        postal_codes = []

        if not text_lower:
            return {"cities": {}, "states": {}, "countries": {}, "postal_codes": []}

        codes = re.findall(r"(?<!\d)([1-9]\d{5})(?!\d)", text_lower)
        if not codes:
            return {"cities": {}, "states": {}, "countries": {}, "postal_codes": []}

        has_location_context = self._has_postal_location_context(text_lower)
        for code in codes:
            match = INDIAN_PIN_PREFIX_TO_LOCATION.get(code[:3])
            if not match:
                continue

            postal_codes.append(code)
            if not has_location_context:
                continue

            city, state = match
            city_counts[city] += 1
            state_counts[state] += 1
            country_counts["India"] += 1

        return {
            "cities": dict(city_counts),
            "states": dict(state_counts),
            "countries": dict(country_counts),
            "postal_codes": postal_codes,
        }

    @staticmethod
    def extract_flag_country_mentions(text):
        countries = Counter()
        if not text:
            return countries

        regional_indicators = []
        for char in str(text):
            codepoint = ord(char)
            if 0x1F1E6 <= codepoint <= 0x1F1FF:
                regional_indicators.append(chr(ord("A") + codepoint - 0x1F1E6))
            else:
                if len(regional_indicators) >= 2:
                    country_code = "".join(regional_indicators[-2:])
                    if pycountry is not None:
                        try:
                            country = pycountry.countries.lookup(country_code)
                            canonical = _canonical_country_name(country.name)
                            if canonical:
                                countries[canonical] += 1
                        except LookupError:
                            pass
                regional_indicators = []

        if len(regional_indicators) >= 2:
            country_code = "".join(regional_indicators[-2:])
            if pycountry is not None:
                try:
                    country = pycountry.countries.lookup(country_code)
                    canonical = _canonical_country_name(country.name)
                    if canonical:
                        countries[canonical] += 1
                except LookupError:
                    pass

        return countries

    def extract_reference_country_mentions(self, text):
        text_lower = self.clean_text(text).lower()
        country_counts = Counter()

        if not text_lower:
            return country_counts

        tokens = re.findall(r"[a-z][a-z.'-]*", text_lower)
        if not tokens:
            return country_counts

        max_words = min(REFERENCE_COUNTRY_MAX_ALIAS_WORDS, len(tokens))
        for size in range(1, max_words + 1):
            for start in range(0, len(tokens) - size + 1):
                phrase = " ".join(tokens[start:start + size])
                country = REFERENCE_COUNTRY_ALIAS_LOOKUP.get(phrase)
                if country:
                    country_counts[country] += 1

        return country_counts

    def extract_geo_mentions(self, text):
        text_lower = self.clean_text(text).lower()
        city_counts = Counter()
        state_counts = Counter()
        country_counts = Counter()
        postal_codes = []

        if not text_lower:
            return {"cities": {}, "states": {}, "countries": {}, "postal_codes": []}

        for city, aliases in CITY_ALIASES.items():
            for alias in aliases:
                if self._contains_phrase(text_lower, alias):
                    city_counts[city] += 1
                    country = CITY_TO_COUNTRY.get(city)
                    if country:
                        country_counts[country] += 1
                    break

        for state, aliases in INDIAN_STATE_ALIASES.items():
            for alias in aliases:
                if self._contains_phrase(text_lower, alias):
                    state_counts[state] += 1
                    if country_counts.get("India", 0) <= 0:
                        country_counts["India"] += 1
                    break

        for country, aliases in COUNTRY_ALIASES.items():
            for alias in aliases:
                if self._contains_phrase(text_lower, alias):
                    normalized_country = self.normalize_country_name(country) or country
                    country_counts[normalized_country] += 1
                    break

        for country, count in self.extract_reference_country_mentions(text_lower).items():
            if country_counts.get(country, 0) <= 0:
                country_counts[country] += count

        for country, count in self.extract_flag_country_mentions(text).items():
            if country_counts.get(country, 0) <= 0:
                country_counts[country] += count

        postal_mentions = self.extract_postal_location_mentions(text_lower)
        for city, count in postal_mentions["cities"].items():
            if city_counts.get(city, 0) <= 0:
                city_counts[city] += count
        for state, count in postal_mentions["states"].items():
            if state_counts.get(state, 0) <= 0:
                state_counts[state] += count
        for country, count in postal_mentions["countries"].items():
            if country_counts.get(country, 0) <= 0:
                country_counts[country] += count
        postal_codes.extend(postal_mentions["postal_codes"])

        return {
            "cities": dict(city_counts),
            "states": dict(state_counts),
            "countries": dict(country_counts),
            "postal_codes": postal_codes,
        }

    @staticmethod
    def normalize_country_name(value):
        if not value:
            return None

        candidate = str(value).strip()
        if not candidate:
            return None

        return _canonical_country_name(candidate)
    
    def count_username_digits(self, username):
        """Count number of digits in username (bot indicator)"""
        if not username:
            return 0
        return sum(c.isdigit() for c in username)
    
    def detect_spam_patterns(self, text):
        """Detect spam patterns in text"""
        if not text:
            return 0
        
        text_lower = text.lower()
        return sum(1 for pattern in SPAM_PATTERNS if pattern in text_lower)
    
    def extract_location_slang(self, text):
        """Extract location-based slang from text"""
        if not text:
            return {}
        
        text_lower = text.lower()
        location_scores = {}
        
        for location, slang_list in LOCATION_SLANG.items():
            score = sum(1 for slang in slang_list if slang in text_lower)
            if score > 0:
                location_scores[location] = score
        
        return location_scores
    
    def analyze_emoji_gender(self, emojis):
        """Analyze emoji usage for gender prediction"""
        if not emojis:
            return {'male': 0, 'female': 0}
        
        male_count = sum(1 for e in emojis if e in MALE_EMOJIS)
        female_count = sum(1 for e in emojis if e in FEMALE_EMOJIS)
        
        return {
            'male': male_count,
            'female': female_count
        }
    
    def extract_gender_keywords(self, text):
        """Extract gender-indicative self-identification keywords.

        Generic address words such as "bhai" or "bro" usually refer to the creator,
        not the commenter, so they should not make the commenter male.
        """
        if not text:
            return {'male': 0, 'female': 0}
        
        text_lower = text.lower()
        male_identity_patterns = [
            r"\bi\s*(am|'m|m)\s*(a\s*)?(man|male|boy|guy|brother|husband|father|dad)\b",
            r"\bas\s*(a\s*)?(man|male|boy|guy|husband|father|dad)\b",
            r"\b(male|boy|guy)\s+here\b",
        ]
        female_identity_patterns = [
            r"\bi\s*(am|'m|m)\s*(a\s*)?(woman|female|girl|lady|sister|wife|mother|mom)\b",
            r"\bas\s*(a\s*)?(woman|female|girl|lady|wife|mother|mom)\b",
            r"\b(female|girl|lady)\s+here\b",
        ]

        male_count = sum(1 for pattern in male_identity_patterns if re.search(pattern, text_lower))
        female_count = sum(1 for pattern in female_identity_patterns if re.search(pattern, text_lower))

        # Keep emoji/word style support for clearly gendered, non-address terms only.
        address_only_male_terms = {"bro", "bhai", "dude", "man", "brother", "king", "beast"}
        address_only_female_terms = {"sis", "queen", "beautiful", "gorgeous", "stunning", "pretty"}
        male_count += sum(
            1 for keyword in MALE_KEYWORDS
            if keyword not in address_only_male_terms and self._contains_phrase(text_lower, keyword)
        )
        female_count += sum(
            1 for keyword in FEMALE_KEYWORDS
            if keyword not in address_only_female_terms and self._contains_phrase(text_lower, keyword)
        )
        
        return {
            'male': male_count,
            'female': female_count
        }
    
    def extract_age_indicators(self, hashtags):
        """Extract age indicators from hashtags"""
        if not hashtags:
            return {}
        
        age_scores = {}
        hashtags_lower = [h.lower().replace('#', '') for h in hashtags]
        
        for age_range, keywords in AGE_HASHTAGS.items():
            score = sum(1 for keyword in keywords if any(keyword in h for h in hashtags_lower))
            if score > 0:
                age_scores[age_range] = score
        
        return age_scores
    
    def extract_timestamp_hour(self, timestamp):
        """Extract hour from timestamp"""
        try:
            if isinstance(timestamp, str):
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            else:
                dt = timestamp
            return dt.hour
        except:
            return None
    
    def is_bot_likely(self, username, comment_text):
        """Determine if user is likely a bot"""
        bot_score = 0
        
        # Check username digits
        if self.count_username_digits(username) >= 4:
            bot_score += 2
        
        # Check spam patterns
        spam_count = self.detect_spam_patterns(comment_text)
        bot_score += spam_count * 2
        
        # Check if emoji only
        if comment_text and self.calculate_emoji_density(comment_text) > 0.9:
            bot_score += 1
        
        # Check if very short with numbers
        if comment_text and len(comment_text) < 5 and any(c.isdigit() for c in comment_text):
            bot_score += 1
        
        return bot_score >= 3
    
    def extract_comment_features(self, comment_data):
        """
        Extract all features from a single comment
        
        Args:
            comment_data: Dict with keys: username, text, timestamp, full_name, profile_pic_url (from RapidAPI)
        
        Returns:
            Dict with extracted features
        """
        username = comment_data.get('username', '')
        text = comment_data.get('text', '')
        timestamp = comment_data.get('timestamp')
        full_name = comment_data.get('full_name', '')  # NEW: From RapidAPI!
        profile_pic_url = comment_data.get('profile_pic_url', '')  # NEW: For face detection!
        normalized_text = self.clean_text(text)
        name_parts = self.parse_name_parts(full_name, username)
        first_name = name_parts["first_name"]
        
        emojis = self.extract_emojis(text)
        emoji_density = self.calculate_emoji_density(text)
        
        # Use username-based language detection for Indian users
        language_details = self.detect_language_details(text)
        language = language_details["language"]
        if language == "unknown":
            language = self.detect_user_language(username, text)
            if language != "unknown":
                language_details = {
                    "language": language,
                    "confidence": 0.35,
                    "candidates": [{"language": language, "confidence": 0.35}],
                }
        
        username_digits = self.count_username_digits(username)
        spam_score = self.detect_spam_patterns(text)
        location_slang = self.extract_location_slang(text)
        geo_mentions = self.extract_geo_mentions(text)
        emoji_gender = self.analyze_emoji_gender(emojis)
        gender_keywords = self.extract_gender_keywords(text)
        is_bot = self.is_bot_likely(username, text)
        hour = self.extract_timestamp_hour(timestamp)
        gender_signal_strength = name_parts["confidence"]
        if sum(emoji_gender.values()) > 0:
            gender_signal_strength += 0.15
        if sum(gender_keywords.values()) > 0:
            gender_signal_strength += 0.15
        gender_signal_strength = min(1.0, gender_signal_strength)
        
        return {
            'username': username,
            'full_name': full_name,  # NEW: Keep full name for reference
            'profile_pic_url': profile_pic_url,  # NEW: Keep profile pic URL for face detection!
            'first_name': first_name,  # Improved: Uses real name when available!
            'last_name': name_parts["last_name"],
            'name_source': name_parts["source"],
            'name_confidence': name_parts["confidence"],
            'text': text,
            'normalized_text': normalized_text,
            'emojis': emojis,
            'emoji_density': emoji_density,
            'language': language,
            'language_confidence': language_details.get('confidence', 0.0),
            'language_candidates': language_details.get('candidates', []),
            'username_digits': username_digits,
            'spam_score': spam_score,
            'location_slang': location_slang,
            'city_mentions': geo_mentions['cities'],
            'state_mentions': geo_mentions['states'],
            'country_mentions': geo_mentions['countries'],
            'postal_codes': geo_mentions['postal_codes'],
            'emoji_gender': emoji_gender,
            'gender_keywords': gender_keywords,
            'gender_signal_strength': gender_signal_strength,
            'is_bot': is_bot,
            'hour': hour,
            'timestamp': timestamp
        }
    
    def extract_post_features(self, post_data):
        """Extract features from a post"""
        caption = post_data.get('caption', '')
        hashtags = post_data.get('hashtags', [])
        location = post_data.get('location')
        timestamp = post_data.get('timestamp')
        
        # Extract age indicators from hashtags
        age_indicators = self.extract_age_indicators(hashtags)
        
        # Extract language
        language = self.detect_language(caption)
        
        return {
            'caption': caption,
            'hashtags': hashtags,
            'location': location,
            'timestamp': timestamp,
            'age_indicators': age_indicators,
            'language': language
        }
