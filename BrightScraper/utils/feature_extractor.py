"""
Layer 2: Feature Extraction
Extract features from comments, posts, and profile data
"""
import re
import emoji
from collections import Counter
from datetime import datetime
from langdetect import detect, LangDetectException

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


class FeatureExtractor:
    """Extract features from Instagram data"""
    
    def __init__(self):
        pass
    
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
            if first_part not in ['the', 'its', 'mr', 'ms', 'dr', 'official', 'real', 'code', 'bit', 'tech', 'dev']:
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
    
    def detect_language(self, text):
        """Detect language of text - IMPROVED for short texts and emojis"""
        if not text or len(text) < 3:
            return 'unknown'
        
        # Remove emojis and special characters for better detection
        import re
        text_clean = re.sub(r'[^\w\s]', '', text)
        text_clean = ''.join(c for c in text_clean if not emoji.is_emoji(c))
        
        # If mostly emojis or too short after cleaning, return unknown
        if len(text_clean.strip()) < 5:
            return 'unknown'
        
        try:
            detected = detect(text_clean)
            # If detected language is uncommon for Instagram India, default to English
            # Common false positives: so (Somali), vi (Vietnamese), pl (Polish), fi (Finnish), sq (Albanian), ca (Catalan), id (Indonesian)
            if detected in ['so', 'vi', 'pl', 'fi', 'nl', 'da', 'no', 'sv', 'sq', 'ca', 'id', 'tl', 'cy']:
                # These are likely misdetections of short English text
                return 'en'
            return detected
        except LangDetectException:
            return 'unknown'
    
    def detect_user_language(self, username, comment_text):
        """
        Detect user's actual language based on username + comment
        For Indian users, detect if they speak Hindi/regional languages even if commenting in English
        """
        # Indian name patterns indicate Hindi/regional language speakers
        indian_name_patterns = {
            'hindi': ['singh', 'kumar', 'sharma', 'gupta', 'yadav', 'verma', 'jain', 'agarwal', 
                     'raj', 'ravi', 'amit', 'ankit', 'rohit', 'rahul', 'deepak', 'sanjay', 'vijay',
                     'priya', 'neha', 'pooja', 'anjali', 'kavya', 'divya', 'arora', 'kapoor',
                     'malhotra', 'bhatia', 'sethi', 'saxena', 'mittal', 'abhi', 'abhishek',
                     'saini', 'tyagi', 'chauhan', 'pandit', 'joshi', 'negi'],
            'tamil': ['raman', 'krishnan', 'murugan', 'sundaram', 'rajesh', 'iyer', 'venkat', 'swamy'],
            'telugu': ['reddy', 'rao', 'naidu', 'prasad', 'chowdary'],
            'kannada': ['gowda', 'hegde', 'shetty', 'nayak'],
            'bengali': ['das', 'sen', 'chatterjee', 'banerjee', 'ghosh', 'bose', 'roy', 'dutta'],
            'marathi': ['patil', 'kulkarni', 'deshmukh', 'pawar', 'shinde', 'jadhav']
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
            hindi_words = ['bhai', 'yaar', 'kya', 'acha', 'matlab', 'dost', 'bhidu', 'guru', 
                          'anna', 'da', 'machaa', 'dekh', 'sahi', 'bro', 'dude', 'bhai', 'vai']
            if any(word in text_lower for word in hindi_words):
                return 'hindi'
        
        # Fallback to regular detection
        detected = self.detect_language(comment_text) if comment_text else 'en'
        
        # If still unknown, default to English (most common on Instagram India)
        if detected == 'unknown':
            return 'en'
        
        return detected
    
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
        """Extract gender-indicative keywords"""
        if not text:
            return {'male': 0, 'female': 0}
        
        text_lower = text.lower()
        
        male_count = sum(1 for keyword in MALE_KEYWORDS if keyword in text_lower)
        female_count = sum(1 for keyword in FEMALE_KEYWORDS if keyword in text_lower)
        
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
        
        # Extract first name - prefer full_name from Apify, fallback to username parsing
        if full_name and full_name.strip():
            # Use first part of full name (e.g., "Abhi Shek" -> "Abhi")
            first_name = full_name.strip().split()[0].lower()
        else:
            # Fallback to extracting from username
            first_name = self.extract_first_name(username)
        
        emojis = self.extract_emojis(text)
        emoji_density = self.calculate_emoji_density(text)
        
        # Use username-based language detection for Indian users
        language = self.detect_user_language(username, text)
        
        username_digits = self.count_username_digits(username)
        spam_score = self.detect_spam_patterns(text)
        location_slang = self.extract_location_slang(text)
        emoji_gender = self.analyze_emoji_gender(emojis)
        gender_keywords = self.extract_gender_keywords(text)
        is_bot = self.is_bot_likely(username, text)
        hour = self.extract_timestamp_hour(timestamp)
        
        return {
            'username': username,
            'full_name': full_name,  # NEW: Keep full name for reference
            'profile_pic_url': profile_pic_url,  # NEW: Keep profile pic URL for face detection!
            'first_name': first_name,  # Improved: Uses real name when available!
            'text': text,
            'emojis': emojis,
            'emoji_density': emoji_density,
            'language': language,
            'username_digits': username_digits,
            'spam_score': spam_score,
            'location_slang': location_slang,
            'emoji_gender': emoji_gender,
            'gender_keywords': gender_keywords,
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
