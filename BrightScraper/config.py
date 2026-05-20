"""
Configuration file for Instagram Audience Analytics System
"""

# BrightData Configuration
INSTAGRAM_PROFILE_DATASET_ID = 'gd_l1vikfch901nx3by4'
INSTAGRAM_COMMENTS_DATASET_ID = 'gd_l7q7dkns168ruy1di'  # You'll need to get this from BrightData

# Feature Extraction Configuration
AGE_HASHTAGS = {
    '13-17': ['school', 'schoollife', 'student', 'teenager', 'teen', 'homework', 'exam'],
    '18-24': ['college', 'university', 'collegelife', 'student', 'grad', 'party', 'nightout'],
    '25-34': ['work', 'office', 'career', 'professional', 'entrepreneur', 'startup', 'gym'],
    '35-44': ['family', 'parent', 'momlife', 'dadlife', 'kids', 'marriage', 'home'],
    '45+': ['grandparent', 'retired', 'senior', 'mature', 'wisdom']
}

# Slang to Location Mapping
LOCATION_SLANG = {
    'India': ['bhai', 'yaar', 'dost', 'kya', 'acha', 'thik', 'matlab'],
    'Bangalore': ['machaa', 'guru', 'swalpa'],
    'Mumbai': ['bhidu', 'mamu', 'tapori'],
    'Delhi': ['yaar', 'bro', 'bhai'],
    'Hyderabad': ['anna', 'bhai'],
    'Chennai': ['da', 'machaa', 'pa'],
    'Malaysia': ['lah', 'leh', 'lor'],
    'Singapore': ['lah', 'lor', 'sia'],
    'UAE': ['habibi', 'mashallah', 'inshallah'],
    'USA': ['dude', 'bro', 'man', 'buddy'],
    'UK': ['mate', 'lad', 'cheers', 'innit']
}

# Spam/Bot Detection Patterns
SPAM_PATTERNS = [
    'check dm', 'follow back', 'follow me', 'dm me',
    '💯💯💯', 'click link', 'click bio', 'check profile',
    'free followers', 'earn money', 'make money'
]

# Emoji Analysis
MALE_EMOJIS = ['🔥', '💪', '⚽', '🏀', '🎮', '👊', '🚀', '💰']
FEMALE_EMOJIS = ['💕', '💖', '✨', '🌸', '🦋', '💅', '👗', '💄', '🌺']

# Gender Keywords
MALE_KEYWORDS = ['bro', 'bhai', 'dude', 'man', 'brother', 'king', 'beast']
FEMALE_KEYWORDS = ['sis', 'girl', 'queen', 'gorgeous', 'beautiful', 'stunning', 'pretty']

# Timezone to Country Mapping (IST reference)
TIMEZONE_COUNTRY = {
    'India': {'peak_hours': [7, 8, 9, 10, 17, 18, 19, 20, 21, 22]},  # IST
    'USA': {'peak_hours': [23, 0, 1, 2, 3, 11, 12]},  # EST/PST in IST
    'UAE': {'peak_hours': [9, 10, 11, 12, 18, 19, 20, 21]},  # GST in IST
    'UK': {'peak_hours': [13, 14, 15, 16, 17, 20, 21, 22]},  # GMT in IST
    'Singapore': {'peak_hours': [8, 9, 10, 11, 17, 18, 19, 20]},  # SGT in IST
}

# ML Model Configuration
GENDER_WEIGHT = {
    'name': 0.8,  # Increased - name is most reliable
    'comment_style': 0.1,
    'emoji_style': 0.1
}

COUNTRY_WEIGHT = {
    'language': 0.7,  # Increased - language is most reliable for country
    'geotag': 0.2,    # Decreased - often not available
    'slang': 0.05,
    'timezone': 0.05  # Decreased - unreliable
}

CITY_WEIGHT = {
    'geotag': 0.6,  # Increased - most reliable when available
    'slang': 0.3,   # Increased
    'bio': 0.1,     # Decreased
    'timezone': 0.0  # Removed - not useful for city
}

# Audience Quality Score Configuration
AQ_SCORE_WEIGHT = {
    'fake_follower_ratio': 0.4,
    'engagement_quality': 0.4,
    'sentiment_score': 0.2
}

# Bot Detection Thresholds
BOT_DETECTION = {
    'username_digit_threshold': 4,
    'spam_phrase_threshold': 2,
    'emoji_only_threshold': 0.9,
    'repeated_comment_threshold': 3
}
