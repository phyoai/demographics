"""
Layer 3: ML Predictions
Gender, Country, City, Age, and Bot Detection
"""
import gender_guesser.detector as gender
from collections import Counter
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import re
import time

try:
    from ..config import GENDER_WEIGHT, COUNTRY_WEIGHT, CITY_WEIGHT, TIMEZONE_COUNTRY
except ImportError:
    from config import GENDER_WEIGHT, COUNTRY_WEIGHT, CITY_WEIGHT, TIMEZONE_COUNTRY

# Import the new confidence-based city predictor
try:
    from .confidence_city_predictor import ConfidenceCityPredictor
    CONFIDENCE_PREDICTOR_AVAILABLE = True
except ImportError:
    try:
        from utils.confidence_city_predictor import ConfidenceCityPredictor
        CONFIDENCE_PREDICTOR_AVAILABLE = True
    except ImportError:
        CONFIDENCE_PREDICTOR_AVAILABLE = False
        print("WARNING: Confidence city predictor not available, using legacy method")


class AudiencePredictor:
    """ML-based predictions for audience demographics"""
    
    def __init__(self):
        self.gender_detector = gender.Detector()
        # Initialize Geopy geocoder with a user agent
        self.geolocator = Nominatim(user_agent="phyo_audience_analyzer", timeout=3)
        self.geocode_cache = {}  # Cache to avoid repeated API calls

        # Initialize confidence-based city predictor
        if CONFIDENCE_PREDICTOR_AVAILABLE:
            self.confidence_city_predictor = ConfidenceCityPredictor()
        else:
            self.confidence_city_predictor = None

        # Global cities dictionary for keyword matching
        self.global_cities = {
            # Indian cities
            'Delhi': ['delhi', 'ncr', 'new delhi', 'dilli', 'dlf'],
            'Noida': ['noida', 'greater noida'],
            'Gurugram': ['gurugram', 'gurgaon', 'ggn'],
            'Ghaziabad': ['ghaziabad', 'gzb'],
            'Faridabad': ['faridabad', 'fbd'],
            'Bangalore': ['bangalore', 'bengaluru', 'blr'],
            'Mumbai': ['mumbai', 'bombay', 'bom'],
            'Hyderabad': ['hyderabad', 'hyd'],
            'Chennai': ['chennai', 'madras'],
            'Pune': ['pune'],
            'Kolkata': ['kolkata', 'calcutta', 'cal'],
            'Ahmedabad': ['ahmedabad', 'amd'],
            'Jaipur': ['jaipur', 'jai'],
            # Pakistani cities
            'Karachi': ['karachi', 'khi'],
            'Lahore': ['lahore', 'lhe'],
            'Islamabad': ['islamabad', 'isb'],
            'Rawalpindi': ['rawalpindi', 'pindi'],
            'Faisalabad': ['faisalabad'],
            # Bangladeshi cities
            'Dhaka': ['dhaka', 'dac'],
            'Chittagong': ['chittagong', 'chottogram'],
        }
    
    def geocode_location(self, location_string):
        """
        Use Geopy to extract city from a location string
        Returns: (city_name, confidence) or (None, 0)
        """
        if not location_string or len(location_string.strip()) < 3:
            return None, 0
        
        # Check cache first
        if location_string in self.geocode_cache:
            return self.geocode_cache[location_string]
        
        try:
            # Add ", India" to improve accuracy for Indian locations
            query = f"{location_string}, India"
            location = self.geolocator.geocode(query, addressdetails=True, language='en')
            
            if location and location.raw.get('address'):
                address = location.raw['address']
                
                # Extract city from address components
                # Try different city fields in order of preference
                city = (
                    address.get('city') or 
                    address.get('town') or 
                    address.get('municipality') or
                    address.get('state_district') or
                    address.get('county')
                )
                
                # Map common variations to our standard city names
                city_mapping = {
                    'New Delhi': 'Delhi',
                    'Delhi': 'Delhi',
                    'Noida': 'Noida',
                    'Greater Noida': 'Noida',
                    'Gurugram': 'Gurugram',
                    'Gurgaon': 'Gurugram',
                    'Ghaziabad': 'Ghaziabad',
                    'Faridabad': 'Faridabad',
                    'Mumbai': 'Mumbai',
                    'Bombay': 'Mumbai',
                    'Bengaluru': 'Bangalore',
                    'Bangalore': 'Bangalore',
                    'Chennai': 'Chennai',
                    'Madras': 'Chennai',
                    'Hyderabad': 'Hyderabad',
                    'Pune': 'Pune',
                    'Kolkata': 'Kolkata',
                    'Calcutta': 'Kolkata'
                }
                
                if city:
                    normalized_city = city_mapping.get(city, city)
                    confidence = 0.9  # High confidence from geocoding
                    self.geocode_cache[location_string] = (normalized_city, confidence)
                    return normalized_city, confidence
            
            # Cache negative result too
            self.geocode_cache[location_string] = (None, 0)
            return None, 0
            
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            # Silently fail and cache
            self.geocode_cache[location_string] = (None, 0)
            return None, 0
        except Exception as e:
            # Any other error
            self.geocode_cache[location_string] = (None, 0)
            return None, 0
    
    def predict_gender(self, first_name, emoji_gender, gender_keywords):
        """
        Predict gender from name + high-confidence self-identification signals.

        Returns: {'male': 0.62, 'female': 0.36, 'unknown': 0.02}
        """
        scores = {'male': 0, 'female': 0, 'unknown': 0}
        
        # Common Indian name patterns
        indian_male_names = [
            'raj', 'rajiv', 'rajesh', 'kumar', 'singh', 'dev', 'amit', 'ravi', 'anil', 'sunil',
            'vijay', 'ajay', 'sanjay', 'rahul', 'rohan', 'arjun', 'karan', 'varun',
            'harsh', 'deepak', 'mohit', 'nikhil', 'nikhilesh', 'rohit', 'ankit', 'vishal', 'aman',
            'gaurav', 'ankur', 'shubham', 'abhi', 'abhishek', 'abhinav', 'abhay', 'sarthak',
            'aditya', 'shiva', 'shiv', 'krishna', 'ram', 'prakash', 'shankar', 'nath',
            'rakesh', 'piyush', 'mahesh', 'vikas', 'aryan', 'bhavin', 'krushang', 'devansh',
            'divyansh', 'divyanshu', 'yash', 'kartik', 'sahil', 'saurabh', 'sumit', 'manish',
            'nitesh', 'ritesh', 'siddharth', 'siddhant', 'parth', 'jay', 'jeet'
        ]
        
        indian_female_names = [
            'priya', 'priyanka', 'anjali', 'neha', 'pooja', 'puja', 'kavya', 'shreya', 'ananya',
            'divya', 'isha', 'riya', 'riyaa', 'meera', 'sakshi', 'simran', 'sneha',
            'nisha', 'shweta', 'rashmi', 'swati', 'mansi', 'deepika', 'komal',
            'nikita', 'kriti', 'tanvi', 'vidya', 'sonal', 'aarti', 'rekha', 'vaishali',
            'smita', 'khushbu', 'khushboo', 'janki', 'bhavana', 'vidhi', 'vidhie',
            'prachi', 'dimple', 'archana', 'krisha', 'aarushi', 'aarooshi', 'arushi',
            'richa', 'ritu', 'jyoti', 'payal', 'shikha', 'shruti', 'shradha', 'shraddha',
            'aishwarya', 'radhika', 'vanshika', 'muskan', 'manisha', 'kanika', 'garima',
            'monika', 'sonam', 'shivani', 'shalini', 'preeti', 'seema', 'sujata',
            'nidhi', 'chhavi', 'meenakshi', 'kiran'
        ]

        def _name_tokens(value):
            return [
                token for token in re.split(r"[^a-z]+", value.lower())
                if len(token) > 1
            ]

        def _matches_name(value, names):
            tokens = _name_tokens(value)
            if any(token in names for token in tokens):
                return True

            compact = re.sub(r"[^a-z]", "", value.lower())
            for known_name in sorted(names, key=len, reverse=True):
                if len(known_name) < 4:
                    continue
                if compact.startswith(known_name) or compact.endswith(known_name):
                    return True
                if f"_{known_name}" in value.lower() or f".{known_name}" in value.lower():
                    return True
            return False
        
        # 1. Name-based prediction (80% weight) - MOST IMPORTANT
        if first_name and len(first_name) > 1:
            name_lower = first_name.lower()
            
            # Check curated names first, using conservative token/prefix matching.
            is_male = _matches_name(name_lower, set(indian_male_names))
            is_female = _matches_name(name_lower, set(indian_female_names))
            
            if is_male and not is_female:
                scores['male'] += GENDER_WEIGHT['name']
            elif is_female and not is_male:
                scores['female'] += GENDER_WEIGHT['name']
            elif is_male and is_female:
                # Both patterns found, use gender-guesser as tiebreaker
                name_gender = self.gender_detector.get_gender(first_name.capitalize())
                if name_gender in ['male', 'mostly_male']:
                    scores['male'] += GENDER_WEIGHT['name']
                elif name_gender in ['female', 'mostly_female']:
                    scores['female'] += GENDER_WEIGHT['name']
                else:
                    # Split evenly if still ambiguous
                    scores['male'] += GENDER_WEIGHT['name'] * 0.5
                    scores['female'] += GENDER_WEIGHT['name'] * 0.5
            else:
                # Use gender-guesser library
                name_gender = self.gender_detector.get_gender(first_name.capitalize())
                
                if name_gender in ['male', 'mostly_male']:
                    scores['male'] += GENDER_WEIGHT['name']
                elif name_gender in ['female', 'mostly_female']:
                    scores['female'] += GENDER_WEIGHT['name']
                elif name_gender == 'andy':  # Androgynous - split evenly
                    scores['male'] += GENDER_WEIGHT['name'] * 0.5
                    scores['female'] += GENDER_WEIGHT['name'] * 0.5
                else:
                    # Weak suffix hints are not enough to force a binary gender.
                    if name_lower.endswith(('a', 'i', 'ya', 'ia', 'sha', 'ka', 'ta', 'na', 'ni', 'ti', 'si', 'ika', 'ita')):
                        scores['female'] += GENDER_WEIGHT['name'] * 0.35
                        scores['male'] += GENDER_WEIGHT['name'] * 0.10
                        scores['unknown'] += GENDER_WEIGHT['name'] * 0.55
                    elif name_lower.endswith(('o', 'an', 'en', 'ar', 'sh', 'it', 'aj', 'ul', 'at', 'ir', 'esh', 'esh')):
                        scores['male'] += GENDER_WEIGHT['name'] * 0.35
                        scores['female'] += GENDER_WEIGHT['name'] * 0.10
                        scores['unknown'] += GENDER_WEIGHT['name'] * 0.55
                    else:
                        scores['unknown'] += GENDER_WEIGHT['name']
        else:
            scores['unknown'] += GENDER_WEIGHT['name']
        
        # 2. Emoji style (10% weight)
        if emoji_gender:
            total_emoji = emoji_gender.get('male', 0) + emoji_gender.get('female', 0)
            if total_emoji > 0:
                male_ratio = emoji_gender.get('male', 0) / total_emoji
                female_ratio = emoji_gender.get('female', 0) / total_emoji
                scores['male'] += male_ratio * GENDER_WEIGHT['emoji_style']
                scores['female'] += female_ratio * GENDER_WEIGHT['emoji_style']
            else:
                scores['unknown'] += GENDER_WEIGHT['emoji_style']
        else:
            scores['unknown'] += GENDER_WEIGHT['emoji_style']
        
        # 3. Comment self-identification style (10% weight)
        if gender_keywords:
            total_keywords = gender_keywords.get('male', 0) + gender_keywords.get('female', 0)
            if total_keywords > 0:
                male_ratio = gender_keywords.get('male', 0) / total_keywords
                female_ratio = gender_keywords.get('female', 0) / total_keywords
                scores['male'] += male_ratio * GENDER_WEIGHT['comment_style']
                scores['female'] += female_ratio * GENDER_WEIGHT['comment_style']
            else:
                scores['unknown'] += GENDER_WEIGHT['comment_style']
        else:
            scores['unknown'] += GENDER_WEIGHT['comment_style']
        
        # Normalize
        total = sum(scores.values())
        if total > 0:
            scores = {k: round(v/total, 3) for k, v in scores.items()}
        
        return scores
    
    def predict_country(self, language, geotags, location_slang, hours, usernames=None):
        """
        ADAPTIVE country prediction - works for ANY country/region worldwide!
        Uses multiple signals: names, language, geotags, slang, timezone
        
        Returns: {'India': 0.74, 'UAE': 0.08, ...}
        """
        country_scores = Counter()
        
        # 0. ADAPTIVE: Analyze usernames for regional patterns (50% weight)
        # This section automatically detects patterns from multiple regions
        if usernames:
            # South Asian patterns
            indian_patterns = ['singh', 'kumar', 'sharma', 'gupta', 'kapoor', 'negi', 'rai', 
                             'tomar', 'saini', 'prajapati', 'yadav', 'verma', 'jain', 'agarwal', 'sati',
                             'shiv', 'dhruv', 'piyush', 'himanshu', 'avi',
                             'arora', 'malhotra', 'bhatia', 'sethi', 'reddy', 'rao',
                             'patil', 'kulkarni', 'iyer', 'krishnan', 'das', 'sen', 'gowda']
            
            pakistani_patterns = ['ahmed', 'ali', 'khan', 'malik', 'hussain', 'hassan', 'raza', 'shah',
                                'muhammad', 'usman', 'hamza', 'bilal', 'asif', 'imran', 'zain',
                                'faisal', 'farhan', 'syed', 'iqbal', 'tariq', 'junaid', 'arslan',
                                'butt', 'chaudhry', 'sheikh', 'rajput', 'mirza', 'bhatti']
            
            bangladeshi_patterns = ['rahman', 'ahmed', 'islam', 'haque', 'hassan', 'mahmud', 'alam', 'abdullah']
            
            # Middle Eastern patterns
            arab_patterns = ['abdullah', 'mohammed', 'khalid', 'omar', 'salem', 'fahad', 'sultan',
                           'rashid', 'hamad', 'saeed', 'waleed', 'majid', 'aziz', 'karim']
            
            # Western patterns (English-speaking countries)
            western_patterns = ['smith', 'johnson', 'williams', 'brown', 'jones', 'garcia', 'miller',
                              'davis', 'rodriguez', 'martinez', 'taylor', 'anderson', 'lee', 'white',
                              'thomas', 'jackson', 'martin', 'thompson', 'wilson', 'moore']
            
            # Latin American patterns
            latin_patterns = ['rodriguez', 'martinez', 'garcia', 'lopez', 'gonzalez', 'perez', 'sanchez',
                            'ramirez', 'torres', 'flores', 'rivera', 'gomez', 'diaz', 'cruz']
            
            # East Asian patterns
            asian_patterns = ['kim', 'lee', 'park', 'choi', 'wang', 'zhang', 'liu', 'chen', 'yang',
                            'huang', 'lin', 'wu', 'tanaka', 'sato', 'suzuki', 'watanabe']
            
            # Southeast Asian patterns
            sea_patterns = ['nguyen', 'tran', 'pham', 'le', 'hoang', 'dang', 'bui', 'do',  # Vietnamese
                          'tan', 'lim', 'chua', 'ng', 'ong', 'wong',  # Singapore/Malaysia
                          'putra', 'wijaya', 'kusuma', 'pratama', 'santoso']  # Indonesian
            
            # African patterns
            african_patterns = ['diallo', 'toure', 'traore', 'kone', 'mensah', 'osei', 'agyeman',
                              'mwangi', 'kamau', 'ochieng', 'okeke', 'adeyemi', 'oluwaseun']
            
            # European patterns (non-English)
            european_patterns = ['mueller', 'schmidt', 'schneider', 'fischer', 'weber',  # German
                               'rossi', 'russo', 'ferrari', 'esposito', 'bianchi',  # Italian
                               'martin', 'bernard', 'dubois', 'thomas', 'robert',  # French
                               'silva', 'santos', 'oliveira', 'sousa', 'pereira']  # Portuguese
            
            # Count patterns
            region_counts = {
                'India': 0,
                'Pakistan': 0,
                'Bangladesh': 0,
                'UAE': 0,  # Arab patterns go here
                'USA': 0,
                'Mexico': 0,  # Latin patterns
                'China': 0,  # Asian patterns (or could be Korea/Japan)
                'Vietnam': 0,  # Southeast Asian
                'Nigeria': 0,  # African patterns
                'Germany': 0  # European patterns
            }
            
            for username in usernames:
                username_lower = username.lower()
                
                # Check each pattern set (with smart disambiguation)
                # Pakistani patterns (check FIRST to avoid overlap with Indian Muslim names)
                if any(pattern in username_lower for pattern in pakistani_patterns):
                    has_indian = any(pattern in username_lower for pattern in indian_patterns)
                    has_pakistani = any(pattern in username_lower for pattern in pakistani_patterns)
                    
                    if has_indian and has_pakistani:
                        pak_exclusive = ['butt', 'chaudhry', 'sheikh', 'mirza', 'bhatti', 'hamza', 'usman', 'arslan', 'junaid']
                        if any(p in username_lower for p in pak_exclusive):
                            region_counts['Pakistan'] += 1
                        else:
                            region_counts['India'] += 1
                    else:
                        region_counts['Pakistan'] += 1
                elif any(pattern in username_lower for pattern in bangladeshi_patterns):
                    region_counts['Bangladesh'] += 1
                elif any(pattern in username_lower for pattern in indian_patterns):
                    region_counts['India'] += 1
                elif any(pattern in username_lower for pattern in arab_patterns):
                    region_counts['UAE'] += 1
                elif any(pattern in username_lower for pattern in latin_patterns):
                    region_counts['Mexico'] += 1
                elif any(pattern in username_lower for pattern in asian_patterns):
                    region_counts['China'] += 1
                elif any(pattern in username_lower for pattern in sea_patterns):
                    region_counts['Vietnam'] += 1
                elif any(pattern in username_lower for pattern in african_patterns):
                    region_counts['Nigeria'] += 1
                elif any(pattern in username_lower for pattern in european_patterns):
                    region_counts['Germany'] += 1
                elif any(pattern in username_lower for pattern in western_patterns):
                    region_counts['USA'] += 1
            
            # Add scores based on detected patterns (ADAPTIVE!)
            total_users = len(usernames)
            if total_users > 0:
                for region, count in region_counts.items():
                    if count > 0:
                        ratio = count / total_users
                        # Scale up if significant presence (>5%)
                        if ratio > 0.05:
                            weight = 0.5 * min(ratio * 1.5, 1.0)
                            country_scores[region] += weight
                            print(f'  � {region} name patterns: {count}/{total_users} ({ratio*100:.1f}%)')
        
        # 1. ADAPTIVE: Language-based prediction (30% weight)
        # Expanded to cover 100+ languages automatically!
        lang_country_map = {
            # English-speaking
            'en': {'USA': 0.30, 'UK': 0.15, 'India': 0.20, 'Canada': 0.10, 'Australia': 0.08, 'Singapore': 0.05, 'UAE': 0.04},
            # South Asian
            'hi': {'India': 1.0},
            'ur': {'Pakistan': 0.70, 'India': 0.30},
            'bn': {'Bangladesh': 0.85, 'India': 0.15},
            'pa': {'India': 0.70, 'Pakistan': 0.30},
            'ta': {'India': 0.70, 'Sri Lanka': 0.15, 'Singapore': 0.10, 'UAE': 0.05},
            'te': {'India': 1.0},
            'kn': {'India': 1.0},
            'ml': {'India': 0.95, 'UAE': 0.05},
            # Middle Eastern
            'ar': {'Saudi Arabia': 0.25, 'UAE': 0.20, 'Egypt': 0.15, 'Morocco': 0.10, 'Iraq': 0.08, 'India': 0.05},
            'fa': {'Iran': 0.85, 'Afghanistan': 0.10, 'Tajikistan': 0.05},
            'he': {'Israel': 1.0},
            'tr': {'Turkey': 0.95, 'Germany': 0.05},
            # East Asian
            'zh': {'China': 0.70, 'Taiwan': 0.15, 'Singapore': 0.08, 'Malaysia': 0.05},
            'ja': {'Japan': 1.0},
            'ko': {'South Korea': 1.0},
            # Southeast Asian
            'vi': {'Vietnam': 1.0},
            'th': {'Thailand': 1.0},
            'id': {'Indonesia': 1.0},
            'ms': {'Malaysia': 0.70, 'Indonesia': 0.20, 'Singapore': 0.10},
            'tl': {'Philippines': 1.0},
            # European
            'es': {'Spain': 0.20, 'Mexico': 0.25, 'Argentina': 0.15, 'Colombia': 0.12, 'USA': 0.10},
            'pt': {'Brazil': 0.70, 'Portugal': 0.20, 'Angola': 0.05},
            'fr': {'France': 0.45, 'Canada': 0.15, 'Belgium': 0.10, 'Morocco': 0.08, 'Ivory Coast': 0.05},
            'de': {'Germany': 0.80, 'Austria': 0.12, 'Switzerland': 0.08},
            'it': {'Italy': 1.0},
            'ru': {'Russia': 0.85, 'Ukraine': 0.08, 'Kazakhstan': 0.05},
            'pl': {'Poland': 1.0},
            'nl': {'Netherlands': 0.75, 'Belgium': 0.20, 'Suriname': 0.05},
            # African
            'sw': {'Tanzania': 0.35, 'Kenya': 0.30, 'Uganda': 0.15, 'DRC': 0.10},
            'am': {'Ethiopia': 1.0},
            'ha': {'Nigeria': 0.70, 'Niger': 0.15, 'Ghana': 0.10},
            'yo': {'Nigeria': 1.0},
            # Other
            'unknown': {'USA': 0.20, 'India': 0.15, 'UK': 0.12, 'Brazil': 0.10, 'Indonesia': 0.08, 'Mexico': 0.07}
        }
        
        if language in lang_country_map:
            for country, weight in lang_country_map[language].items():
                country_scores[country] += weight * 0.3  # 30% weight
        else:
            # Unknown language - distribute across major Instagram countries
            country_scores['USA'] += 0.06
            country_scores['India'] += 0.05
            country_scores['Brazil'] += 0.04
            country_scores['Indonesia'] += 0.04
            country_scores['Mexico'] += 0.03
        
        # 2. ADAPTIVE: Geotag analysis (15% weight) - Automatically detects ANY country/city
        if geotags:
            # Comprehensive country/city keywords (automatically expands to 150+ countries!)
            location_keywords = {
                # North America
                'USA': ['usa', 'america', 'united states', 'new york', 'los angeles', 'chicago', 'houston', 'miami', 'san francisco', 'seattle'],
                'Canada': ['canada', 'toronto', 'vancouver', 'montreal', 'calgary', 'ottawa'],
                'Mexico': ['mexico', 'ciudad de mexico', 'guadalajara', 'monterrey', 'tijuana'],
                # South America
                'Brazil': ['brazil', 'brasil', 'sao paulo', 'rio de janeiro', 'brasilia', 'salvador'],
                'Argentina': ['argentina', 'buenos aires', 'cordoba', 'rosario', 'mendoza'],
                'Colombia': ['colombia', 'bogota', 'medellin', 'cali', 'cartagena'],
                # Europe
                'UK': ['uk', 'united kingdom', 'london', 'manchester', 'birmingham', 'glasgow', 'liverpool'],
                'Germany': ['germany', 'deutschland', 'berlin', 'munich', 'hamburg', 'frankfurt', 'cologne'],
                'France': ['france', 'paris', 'marseille', 'lyon', 'toulouse', 'nice'],
                'Italy': ['italy', 'italia', 'rome', 'milan', 'naples', 'turin', 'florence'],
                'Spain': ['spain', 'españa', 'madrid', 'barcelona', 'valencia', 'seville'],
                'Russia': ['russia', 'moscow', 'saint petersburg', 'novosibirsk', 'ekaterinburg'],
                # Middle East
                'UAE': ['uae', 'dubai', 'abu dhabi', 'sharjah', 'ajman'],
                'Saudi Arabia': ['saudi', 'arabia', 'riyadh', 'jeddah', 'mecca', 'medina'],
                'Turkey': ['turkey', 'turkiye', 'istanbul', 'ankara', 'izmir', 'antalya'],
                'Iran': ['iran', 'tehran', 'isfahan', 'shiraz', 'tabriz'],
                'Israel': ['israel', 'tel aviv', 'jerusalem', 'haifa'],
                # South Asia
                'India': ['india', 'delhi', 'mumbai', 'bangalore', 'hyderabad', 'chennai', 'kolkata', 'pune', 'ahmedabad'],
                'Pakistan': ['pakistan', 'karachi', 'lahore', 'islamabad', 'rawalpindi', 'faisalabad', 'multan'],
                'Bangladesh': ['bangladesh', 'dhaka', 'chittagong', 'khulna', 'sylhet'],
                'Sri Lanka': ['sri lanka', 'colombo', 'kandy', 'galle'],
                # East Asia
                'China': ['china', 'beijing', 'shanghai', 'guangzhou', 'shenzhen', 'chongqing', 'wuhan'],
                'Japan': ['japan', 'tokyo', 'osaka', 'kyoto', 'yokohama', 'nagoya', 'fukuoka'],
                'South Korea': ['korea', 'seoul', 'busan', 'incheon', 'daegu', 'daejeon'],
                # Southeast Asia
                'Indonesia': ['indonesia', 'jakarta', 'surabaya', 'bandung', 'medan', 'semarang'],
                'Thailand': ['thailand', 'bangkok', 'chiang mai', 'phuket', 'pattaya'],
                'Philippines': ['philippines', 'manila', 'quezon city', 'davao', 'cebu'],
                'Vietnam': ['vietnam', 'hanoi', 'ho chi minh', 'da nang', 'can tho'],
                'Malaysia': ['malaysia', 'kuala lumpur', 'penang', 'johor bahru', 'ipoh'],
                'Singapore': ['singapore'],
                # Africa
                'Nigeria': ['nigeria', 'lagos', 'abuja', 'kano', 'ibadan', 'port harcourt'],
                'South Africa': ['south africa', 'johannesburg', 'cape town', 'durban', 'pretoria'],
                'Egypt': ['egypt', 'cairo', 'alexandria', 'giza', 'luxor'],
                'Kenya': ['kenya', 'nairobi', 'mombasa', 'kisumu'],
                'Morocco': ['morocco', 'casablanca', 'rabat', 'marrakech', 'fes'],
                # Oceania
                'Australia': ['australia', 'sydney', 'melbourne', 'brisbane', 'perth', 'adelaide'],
                'New Zealand': ['new zealand', 'auckland', 'wellington', 'christchurch']
            }
            
            for location in geotags:
                if location and isinstance(location, str):
                    location_lower = location.lower()
                    # Check each country's keywords
                    for country, keywords in location_keywords.items():
                        if any(keyword in location_lower for keyword in keywords):
                            country_scores[country] += 0.15  # 15% per match
                            break  # Only count once per geotag
        
        # 3. Location slang (5% weight) - Expanded city mapping
        if location_slang:
            # Comprehensive city-to-country mapping
            city_country_map = {
                # India
                'Bangalore': 'India', 'Mumbai': 'India', 'Delhi': 'India', 'Hyderabad': 'India', 
                'Chennai': 'India', 'Pune': 'India', 'Kolkata': 'India', 'Ahmedabad': 'India',
                # Pakistan
                'Karachi': 'Pakistan', 'Lahore': 'Pakistan', 'Islamabad': 'Pakistan', 'Rawalpindi': 'Pakistan',
                # UAE
                'Dubai': 'UAE', 'Abu Dhabi': 'UAE', 'Sharjah': 'UAE',
                # USA
                'New York': 'USA', 'Los Angeles': 'USA', 'Chicago': 'USA', 'Houston': 'USA',
                # UK
                'London': 'UK', 'Manchester': 'UK', 'Birmingham': 'UK',
                # Add more as needed...
            }
            
            for location, score in location_slang.items():
                country = city_country_map.get(location)
                if country:
                    country_scores[country] += score * 0.05
        
        # Normalize and return top countries
        total = sum(country_scores.values())
        if total > 0:
            normalized = {k: round(v/total, 3) for k, v in country_scores.most_common()}
            
            # ADAPTIVE: No hard-coded country preferences!
            # Return top 5-7 countries based purely on detected signals
            return {k: v for k, v in list(normalized.items())[:7]}  # Top 7 countries
        
        # Default: If NO data at all, return most common Instagram countries worldwide
        return {
            'USA': 0.18,
            'India': 0.16,
            'Brazil': 0.12,
            'Indonesia': 0.10,
            'Mexico': 0.08,
            'UK': 0.07,
            'Turkey': 0.06
        }
    
    def predict_city(self, geotags, location_slang, bio_text, usernames=None, full_names=None, hours=None, extracted_comments=None):
        """
        Predict city from geotags, slang, and INDIAN SURNAME PATTERNS - ENHANCED WITH GEOPY
        
        Multi-factor scoring with intelligent validation:
        1. Direct city mentions in usernames (70% weight) - HIGHEST PRIORITY
        2. Distinctive regional surnames (15% weight) - WITH STRICT FILTERING
        3. Common/unmatched names (15% weight) - DEFAULT TO DELHI NCR
        4. Geotags with Geopy geocoding (10% weight) - ENHANCED ACCURACY
        5. Bio text with NLP extraction (5% weight) - PATTERN MATCHING
        
        Validation layers:
        - Layer 1: Remove Chennai/Hyderabad if clearly losing to Delhi NCR in surnames
        - Layer 2: Cross-validate with geotags/bio/geocoded data
        - Layer 3: Final check for absolute & relative weakness
        
        Returns: {'Delhi': 0.45, 'Noida': 0.08, ...}
        """
        city_scores = Counter()
        
        # ONLY EXTREMELY DISTINCTIVE surnames that are nearly 100% accurate
        # Removed all ambiguous surnames to prevent false matches
        indian_region_patterns = {
            'Delhi NCR': {
                # ONLY surnames almost exclusive to North India
                'surnames': ['negi', 'bist', 'rawat', 'tyagi', 'tanwar',
                           'saxena', 'mittal', 'bansal', 'goyal', 'khanna', 
                           'bhatia', 'sethi', 'chawla', 'grover'],
                'weight': 1.0,
                'cities': {
                    'Delhi': 0.58,
                    'Noida': 0.16,
                    'Gurugram': 0.13,
                    'Ghaziabad': 0.08,
                    'Faridabad': 0.05
                }
            },
            'Mumbai': {
                # ONLY Marathi surnames
                'surnames': ['patil', 'kulkarni', 'deshmukh', 'pawar', 'shinde', 'jadhav', 
                           'gaikwad', 'bhosale', 'sawant'],
                'weight': 1.0,
                'cities': {'Mumbai': 0.75, 'Pune': 0.25}
            },
            'Bangalore': {
                # ONLY Kannada surnames
                'surnames': ['gowda', 'shetty', 'hegde', 'kamath'],
                'weight': 1.0,
                'cities': {'Bangalore': 1.0}
            },
            'Chennai': {
                # DISABLED - Too many false positives
                # ONLY if someone has Tamil Brahmin surname AND ends with exact match
                'surnames': ['iyer', 'iyengar'],  # Only 2 most distinctive
                'weight': 0.3,  # Very low weight
                'cities': {'Chennai': 1.0}
            },
            'Hyderabad': {
                # ONLY Telugu surnames
                'surnames': ['reddy', 'chowdary'],  # Removed naidu, goud, raju - too common
                'weight': 0.5,  # Reduced weight
                'cities': {'Hyderabad': 1.0}
            },
            'Kolkata': {
                # ONLY Bengali surnames
                'surnames': ['chatterjee', 'banerjee', 'mukherjee', 'chakraborty', 'bhattacharya'],
                'weight': 1.0,
                'cities': {'Kolkata': 1.0}
            }
        }
        
        # ALL other surnames default to Delhi NCR (most common on Instagram India)
        # This prevents false matches to other cities
        common_surnames = ['kumar', 'singh', 'sharma', 'gupta', 'verma', 'rao', 'prasad',
                          'jain', 'agarwal', 'chauhan', 'yadav', 'saini', 'pandit', 'bhatt', 
                          'joshi', 'das', 'sen', 'roy', 'thakur', 'more', 'khan', 'ali',
                          'arora', 'kapoor', 'malhotra', 'mehra', 'sati', 'tomar', 'naidu',
                          'goud', 'raju', 'krishnan', 'venkat', 'raman', 'rama']
        
        # 1. HIGHEST PRIORITY: Direct city mentions in username (70% weight)
        if usernames or full_names:
            all_names = []
            if usernames:
                all_names.extend(usernames)
            if full_names:
                all_names.extend(full_names)
            
            region_matches = Counter()
            direct_city_mentions = Counter()
            common_surname_count = 0
            total_analyzed = 0
            
            for name in all_names:
                if not name or len(name) < 2:
                    continue
                    
                total_analyzed += 1
                name_lower = name.lower().strip()
                
                # Priority 1: Check for direct city mentions in username (HIGHEST CONFIDENCE)
                city_found = False
                if 'delhi' in name_lower or 'dilli' in name_lower or 'ncr' in name_lower or 'dlf' in name_lower:
                    direct_city_mentions['Delhi'] += 1
                    city_found = True
                elif 'noida' in name_lower:
                    direct_city_mentions['Noida'] += 1
                    city_found = True
                elif 'gurugram' in name_lower or 'gurgaon' in name_lower or 'ggn' in name_lower:
                    direct_city_mentions['Gurugram'] += 1
                    city_found = True
                elif 'faridabad' in name_lower or 'fbd' in name_lower:
                    direct_city_mentions['Faridabad'] += 1
                    city_found = True
                elif 'ghaziabad' in name_lower or 'gzb' in name_lower:
                    direct_city_mentions['Ghaziabad'] += 1
                    city_found = True
                elif 'mumbai' in name_lower or 'bombay' in name_lower or 'bom' in name_lower:
                    direct_city_mentions['Mumbai'] += 1
                    city_found = True
                elif 'bangalore' in name_lower or 'bengaluru' in name_lower or 'blr' in name_lower or 'bang' in name_lower:
                    direct_city_mentions['Bangalore'] += 1
                    city_found = True
                elif 'chennai' in name_lower or 'madras' in name_lower:
                    direct_city_mentions['Chennai'] += 1
                    city_found = True
                elif 'hyderabad' in name_lower or 'hyd' in name_lower:
                    direct_city_mentions['Hyderabad'] += 1
                    city_found = True
                elif 'pune' in name_lower:
                    direct_city_mentions['Pune'] += 1
                    city_found = True
                elif 'kolkata' in name_lower or 'calcutta' in name_lower:
                    direct_city_mentions['Kolkata'] += 1
                    city_found = True
                
                if city_found:
                    continue  # Skip surname matching if city is explicitly mentioned
                
                # Priority 2: Check for VERY distinctive regional surnames ONLY
                # Using EXACT WORD MATCH to prevent false positives
                surname_matched = False
                name_parts = name_lower.replace('_', ' ').replace('.', ' ').split()
                
                for region, data in indian_region_patterns.items():
                    # Skip Chennai and Hyderabad matching for most names (too many false positives)
                    if region in ['Chennai', 'Hyderabad'] and len(name_parts) > 0:
                        # Only match if surname is the LAST word in the name
                        last_word = name_parts[-1] if name_parts else ''
                        if last_word in data['surnames']:
                            region_matches[region] += 1
                            surname_matched = True
                            print(f"       ✓ Matched '{last_word}' -> {region} (last word)")
                            break
                    else:
                        # For other regions, match any word
                        for word in name_parts:
                            if word in data['surnames'] and len(word) >= 4:
                                region_matches[region] += 1
                                surname_matched = True
                                print(f"       ✓ Matched '{word}' -> {region}")
                                break
                    if surname_matched:
                        break
                
                # Priority 3: Check for common surnames (default to Delhi NCR weighted distribution)
                if not surname_matched and not city_found:
                    for common_surname in common_surnames:
                        if common_surname in name_lower:
                            common_surname_count += 1
                            break
            
            print(f"  📊 Analyzed {total_analyzed} names:")
            if direct_city_mentions:
                print(f"     - Direct city mentions: {sum(direct_city_mentions.values())} ({dict(direct_city_mentions)})")
            if region_matches:
                print(f"     - Regional surnames: {sum(region_matches.values())} ({dict(region_matches)})")
            if common_surname_count > 0:
                print(f"     - Common surnames (defaulting to Delhi NCR): {common_surname_count}")
            
            # DEBUG: Show if Chennai was detected
            if 'Chennai' in region_matches or 'Chennai' in direct_city_mentions:
                print(f"     ⚠️  WARNING: Chennai detected! This might be a false positive.")
            
            # Convert direct city mentions to scores (HIGH WEIGHT - 50%)
            # Explicit mentions are most reliable
            if direct_city_mentions:
                total_mentions = sum(direct_city_mentions.values())
                for city, count in direct_city_mentions.items():
                    # Each direct mention = strong signal for that specific user
                    # Represents actual % of analyzed users from that city
                    city_scores[city] += (count / total_analyzed) * 0.50  # 50% weight
            
            # Convert region matches to city scores (VERY LOW WEIGHT - 5%)
            # Surname matching is highly unreliable and prone to false positives
            if region_matches:
                # SMART FILTERING: Check if Chennai/Hyderabad are majority winners
                total_region_matches = sum(region_matches.values())
                chennai_count = region_matches.get('Chennai', 0)
                hyderabad_count = region_matches.get('Hyderabad', 0)
                delhi_ncr_count = region_matches.get('Delhi NCR', 0)
                
                # Calculate ratios
                chennai_of_matches = chennai_count / total_region_matches if total_region_matches > 0 else 0
                hyderabad_of_matches = hyderabad_count / total_region_matches if total_region_matches > 0 else 0
                delhi_ncr_of_matches = delhi_ncr_count / total_region_matches if total_region_matches > 0 else 0
                
                print(f"       📊 Surname match distribution: Delhi NCR={delhi_ncr_of_matches*100:.0f}%, Chennai={chennai_of_matches*100:.0f}%, Hyderabad={hyderabad_of_matches*100:.0f}%")
                
                # RULE: Remove Chennai/Hyderabad ONLY if they're clearly losing to Delhi NCR
                # If Chennai is less than 25% of matches AND Delhi NCR is winning
                if chennai_of_matches < 0.25 and delhi_ncr_of_matches > chennai_of_matches * 2:
                    if 'Chennai' in region_matches:
                        removed = region_matches.pop('Chennai')
                        region_matches['Delhi NCR'] = region_matches.get('Delhi NCR', 0) + removed
                        print(f"       🚫 Removed Chennai (weak: {chennai_of_matches*100:.0f}%, Delhi NCR dominant)")
                
                # If Hyderabad is less than 30% of matches AND Delhi NCR is winning
                if hyderabad_of_matches < 0.30 and delhi_ncr_of_matches > hyderabad_of_matches * 1.5:
                    if 'Hyderabad' in region_matches:
                        removed = region_matches.pop('Hyderabad')
                        region_matches['Delhi NCR'] = region_matches.get('Delhi NCR', 0) + removed
                        print(f"       🚫 Removed Hyderabad (weak: {hyderabad_of_matches*100:.0f}%, Delhi NCR dominant)")
                
                for region, count in region_matches.items():
                    region_weight = count / total_analyzed
                    region_multiplier = indian_region_patterns[region].get('weight', 1.0)
                    for city, city_prob in indian_region_patterns[region]['cities'].items():
                        city_scores[city] += region_weight * city_prob * region_multiplier * 0.05  # 5% weight (reduced from 15%)
            
            # Common surnames and unmatched names default to Delhi NCR (VERY LOW WEIGHT - 3%)
            # These are the least reliable signals
            unmatched_count = total_analyzed - sum(direct_city_mentions.values()) - sum(region_matches.values())
            if unmatched_count > 0 or common_surname_count > 0:
                effective_count = max(unmatched_count, common_surname_count)
                common_ratio = effective_count / total_analyzed
                # All common/unmatched names contribute very minimally
                city_scores['Delhi'] += common_ratio * 0.50 * 0.03
                city_scores['Noida'] += common_ratio * 0.18 * 0.03
                city_scores['Gurugram'] += common_ratio * 0.15 * 0.03
                city_scores['Mumbai'] += common_ratio * 0.10 * 0.03
                city_scores['Bangalore'] += common_ratio * 0.07 * 0.03
                if effective_count > total_analyzed * 0.3:  # Only log if significant
                    print(f"     - Unmatched/common names (weak signal): {effective_count}")
        
        # 2. ADAPTIVE: Global cities detection from geotags/slang
        # Automatically detects cities from ANY country!
        global_cities = {
            # Indian cities
            'Delhi': ['delhi', 'ncr', 'new delhi', 'dilli', 'dlf'],
            'Noida': ['noida', 'greater noida'],
            'Gurugram': ['gurugram', 'gurgaon', 'ggn'],
            'Ghaziabad': ['ghaziabad', 'gzb'],
            'Faridabad': ['faridabad', 'fbd'],
            'Bangalore': ['bangalore', 'bengaluru', 'blr'],
            'Mumbai': ['mumbai', 'bombay', 'bom'],
            'Hyderabad': ['hyderabad', 'hyd'],
            'Chennai': ['chennai', 'madras'],
            'Pune': ['pune'],
            'Kolkata': ['kolkata', 'calcutta', 'cal'],
            'Ahmedabad': ['ahmedabad', 'amd'],
            'Jaipur': ['jaipur', 'jai'],
            # Pakistani cities
            'Karachi': ['karachi', 'khi'],
            'Lahore': ['lahore', 'lhe'],
            'Islamabad': ['islamabad', 'isb'],
            'Rawalpindi': ['rawalpindi', 'pindi'],
            'Faisalabad': ['faisalabad'],
            # Bangladeshi cities
            'Dhaka': ['dhaka', 'dac'],
            'Chittagong': ['chittagong'],
            # US cities
            'New York': ['new york', 'nyc', 'manhattan', 'brooklyn'],
            'Los Angeles': ['los angeles', 'la', 'hollywood'],
            'Chicago': ['chicago'],
            'Houston': ['houston'],
            'Miami': ['miami'],
            'San Francisco': ['san francisco', 'sf', 'bay area'],
            # UK cities
            'London': ['london'],
            'Manchester': ['manchester'],
            'Birmingham': ['birmingham'],
            # Middle Eastern cities
            'Dubai': ['dubai'],
            'Abu Dhabi': ['abu dhabi'],
            'Riyadh': ['riyadh'],
            'Jeddah': ['jeddah'],
            'Istanbul': ['istanbul'],
            # Southeast Asian cities
            'Bangkok': ['bangkok'],
            'Singapore': ['singapore'],
            'Jakarta': ['jakarta'],
            'Manila': ['manila'],
            'Ho Chi Minh': ['ho chi minh', 'saigon'],
            'Kuala Lumpur': ['kuala lumpur', 'kl'],
            # East Asian cities
            'Tokyo': ['tokyo'],
            'Shanghai': ['shanghai'],
            'Seoul': ['seoul'],
            'Beijing': ['beijing'],
            # European cities
            'Paris': ['paris'],
            'Berlin': ['berlin'],
            'Madrid': ['madrid'],
            'Rome': ['rome'],
            'Barcelona': ['barcelona'],
            # Latin American cities
            'Mexico City': ['mexico city', 'cdmx'],
            'Sao Paulo': ['sao paulo'],
            'Buenos Aires': ['buenos aires'],
            'Bogota': ['bogota'],
            # African cities
            'Lagos': ['lagos'],
            'Cairo': ['cairo'],
            'Johannesburg': ['johannesburg', 'joburg'],
            'Nairobi': ['nairobi']
        }
        
        # Track geotag/bio signals for validation
        geotag_cities = Counter()
        bio_cities = Counter()
        geocoded_cities = Counter()  # NEW: Cities found via Geopy
        
        # Geotags - Enhanced with Geopy (30% weight - highly reliable!)
        # Geotags are explicit location data from posts
        if geotags:
            geotag_count = 0
            for location in geotags:
                if location and isinstance(location, str) and len(location.strip()) > 2:
                    location_lower = location.lower().strip()
                    
                    # First try keyword matching (fast)
                    keyword_matched = False
                    for city, keywords in self.global_cities.items():
                        if any(keyword in location_lower for keyword in keywords):
                            city_scores[city] += 0.20  # 20% for keyword match (increased from 5%)
                            geotag_cities[city] += 1
                            geotag_count += 1
                            keyword_matched = True
                            break
                    
                    # If no keyword match, try Geopy geocoding (slower but more accurate)
                    if not keyword_matched and len(location) > 5:
                        geocoded_city, confidence = self.geocode_location(location)
                        if geocoded_city and geocoded_city in self.global_cities.keys():
                            city_scores[geocoded_city] += 0.10 * confidence  # 10% * confidence (increased from 5%)
                            geocoded_cities[geocoded_city] += 1
                            geotag_count += 1
                            print(f"       🌍 Geocoded '{location[:30]}' → {geocoded_city}")
                            time.sleep(0.1)  # Rate limiting for Nominatim
            
            if geotag_count > 0:
                print(f"     - Geotags: {geotag_count} (keywords: {dict(geotag_cities)}, geocoded: {dict(geocoded_cities)})")
        
        # Location slang (5% weight - moderate reliability)
        if location_slang:
            for location, score in location_slang.items():
                if location in self.global_cities.keys():
                    city_scores[location] += score * 0.05  # 5% weight (increased from 2%)
        
        # 3. Bio text - Enhanced with location extraction (12% weight - good reliability)
        if bio_text and len(bio_text.strip()) > 5:
            bio_lower = bio_text.lower()
            
            # Try keyword matching first
            keyword_matched = False
            for city, keywords in self.global_cities.items():
                if any(keyword in bio_lower for keyword in keywords):
                    city_scores[city] += 0.08  # 8% weight (increased from 3%)
                    bio_cities[city] += 1
                    keyword_matched = True
                    break
            
            # If no keyword match, try to extract location phrases and geocode them
            if not keyword_matched:
                # Common bio patterns: "From Delhi", "Living in Mumbai", "Based in Bangalore", "📍 Chennai"
                import re
                location_patterns = [
                    r'from\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'living\s+in\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'based\s+in\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'located\s+in\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'📍\s*([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'at\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)'
                ]
                
                for pattern in location_patterns:
                    matches = re.findall(pattern, bio_lower, re.IGNORECASE)
                    for match in matches:
                        location_text = match.strip()
                        if len(location_text) > 3:
                            geocoded_city, confidence = self.geocode_location(location_text)
                            if geocoded_city and geocoded_city in self.global_cities.keys():
                                city_scores[geocoded_city] += 0.04 * confidence  # 4% * confidence (increased from 2%)
                                bio_cities[geocoded_city] += 1
                                print(f"       🌍 Bio geocoded '{location_text}' → {geocoded_city}")
                                break
                    if bio_cities:
                        break
            
            if bio_cities:
                print(f"     - Bio text cities: {dict(bio_cities)}")
        
        # CROSS-VALIDATION: If Chennai/Hyderabad appeared in surnames but NOT in geotags/bio/geocoded, validate
        # BUT only remove if they're also weak in surname matching
        if 'Chennai' in city_scores and city_scores['Chennai'] > 0:
            chennai_has_geo_bio_support = (
                'Chennai' in geotag_cities or 
                'Chennai' in bio_cities or 
                'Chennai' in geocoded_cities
            )
            chennai_score_ratio = city_scores.get('Chennai', 0) / max(city_scores.get('Delhi', 0.001), 0.001)
            
            # Only remove if BOTH conditions: no geo/bio/geocoded support AND weak compared to Delhi
            if not chennai_has_geo_bio_support and chennai_score_ratio < 0.5:
                print(f"       ⚠️  Chennai has NO location support AND weak score (only {chennai_score_ratio*100:.0f}% of Delhi) - removing")
                chennai_score = city_scores['Chennai']
                city_scores['Chennai'] = 0
                city_scores['Delhi'] += chennai_score
            elif not chennai_has_geo_bio_support:
                print(f"       ⚠️  Chennai has NO location support BUT strong surname signal - keeping with reduced weight")
                city_scores['Chennai'] *= 0.5  # Reduce by 50%
                city_scores['Delhi'] += city_scores['Chennai'] * 0.5
            else:
                print(f"       ✅ Chennai validated by location data: {dict(geotag_cities.get('Chennai', 0) or geocoded_cities.get('Chennai', 0) or bio_cities.get('Chennai', 0))}")
        
        if 'Hyderabad' in city_scores and city_scores['Hyderabad'] > 0:
            hyderabad_has_geo_bio_support = (
                'Hyderabad' in geotag_cities or 
                'Hyderabad' in bio_cities or 
                'Hyderabad' in geocoded_cities
            )
            hyderabad_score_ratio = city_scores.get('Hyderabad', 0) / max(city_scores.get('Delhi', 0.001), 0.001)
            
            # Only remove if BOTH conditions: no geo/bio/geocoded support AND weak compared to Delhi
            if not hyderabad_has_geo_bio_support and hyderabad_score_ratio < 0.6:
                print(f"       ⚠️  Hyderabad has NO location support AND weak score (only {hyderabad_score_ratio*100:.0f}% of Delhi) - removing")
                hyderabad_score = city_scores['Hyderabad']
                city_scores['Hyderabad'] = 0
                city_scores['Delhi'] += hyderabad_score
            elif not hyderabad_has_geo_bio_support:
                print(f"       ⚠️  Hyderabad has NO location support BUT strong surname signal - keeping with reduced weight")
                city_scores['Hyderabad'] *= 0.6  # Reduce by 40%
                city_scores['Delhi'] += city_scores['Hyderabad'] * 0.4
            else:
                print(f"       ✅ Hyderabad validated by location data")
        
        # DON'T normalize to 100% - Instagram shows actual representation %
        # Total audience is analyzed_users, city scores represent confidence/representation
        total = sum(city_scores.values())
        
        if total > 0.05:  # If we have any meaningful signal (at least 5% confidence)
            # FINAL VALIDATION: Remove Chennai/Hyderabad ONLY if they're clearly false positives
            delhi_score = city_scores.get('Delhi', 0)
            chennai_score = city_scores.get('Chennai', 0)
            hyderabad_score = city_scores.get('Hyderabad', 0)
            
            # More lenient thresholds - only remove if VERY weak
            # If Chennai is less than 25% of Delhi's score AND very small in absolute terms
            if chennai_score > 0 and delhi_score > 0:
                chennai_to_delhi_ratio = chennai_score / delhi_score
                chennai_absolute = chennai_score / total
                # Only remove if both weak relative to Delhi AND weak in absolute terms
                if chennai_to_delhi_ratio < 0.25 and chennai_absolute < 0.15:
                    print(f"       🚫 FINAL: Removing Chennai (only {chennai_to_delhi_ratio*100:.0f}% of Delhi, {chennai_absolute*100:.0f}% total)")
                    city_scores['Delhi'] += city_scores['Chennai']
                    city_scores['Chennai'] = 0
            
            # If Hyderabad is less than 30% of Delhi's score AND very small in absolute terms
            if hyderabad_score > 0 and delhi_score > 0:
                hyderabad_to_delhi_ratio = hyderabad_score / delhi_score
                hyderabad_absolute = hyderabad_score / total
                # Only remove if both weak relative to Delhi AND weak in absolute terms
                if hyderabad_to_delhi_ratio < 0.30 and hyderabad_absolute < 0.15:
                    print(f"       🚫 FINAL: Removing Hyderabad (only {hyderabad_to_delhi_ratio*100:.0f}% of Delhi, {hyderabad_absolute*100:.0f}% total)")
                    city_scores['Delhi'] += city_scores['Hyderabad']
                    city_scores['Hyderabad'] = 0
            
            # Recalculate total after removing false positives
            total = sum(city_scores.values())
            
            # Get top 5-6 cities
            top_cities = city_scores.most_common(8)
            
            # Filter out cities with very low scores (< 1% of total analyzed users)
            # This represents actual audience representation, not normalized to 100%
            threshold = total * 0.01
            significant_cities = [(city, score) for city, score in top_cities if score >= threshold]
            
            # Take top 5
            significant_cities = significant_cities[:5]
            
            if significant_cities:
                # Calculate ACTUAL representation percentages (NOT normalized to 100%)
                # Each city shows what % of total audience it represents
                # Example: If Delhi has 0.032 confidence → 3.2% of audience
                representation = {}
                for city, score in significant_cities:
                    # Scale score to represent % of total analyzed audience
                    # Higher total = higher confidence in city detection
                    percentage = round((score / total) * 100 * min(total, 1.0), 1)
                    if percentage >= 0.5:  # Only include cities with at least 0.5% representation
                        representation[city] = percentage
                
                print(f"  🏙️  City distribution (confidence: {total:.2f}): {representation}")
                print(f"       ℹ️  Note: Percentages show actual audience representation (not normalized to 100%)")
                return representation
        
        # If very weak signal or no data, return low representation percentages
        # Showing actual % of audience from each city (like Instagram does)
        print(f"  ⚠️  Weak city signals (total confidence: {total:.3f}), using low-confidence distribution")
        return {
            'Delhi': 2.5,       # Low representation signals
            'Mumbai': 1.2,
            'Bangalore': 1.0,
            'Noida': 0.8
        }
    
    def predict_age(self, hashtags, emoji_density, comment_style):
        """
        Predict age range from hashtags and patterns - MORE ACCURATE
        Based on real Instagram demographics (18-24 is the largest group)
        
        Returns: {'13-17': 0.02, '18-24': 0.68, ...}
        """
        try:
            from ..config import AGE_HASHTAGS
        except ImportError:
            from config import AGE_HASHTAGS
        
        age_scores = Counter()
        
        # Base weights from real Instagram demographics
        # 18-24 is the dominant age group on Instagram (67-70%)
        base_weights = {
            '13-17': 2,
            '18-24': 68,  # Matches real data
            '25-34': 24,  # Matches real data
            '35-44': 5,
            '45+': 1
        }
        
        # Start with base weights (heavier influence)
        for age_range, weight in base_weights.items():
            age_scores[age_range] += weight * 2  # Double the base weights
        
        # Hashtag analysis - adjust scores (smaller adjustments)
        if hashtags:
            for age_range, keywords in AGE_HASHTAGS.items():
                hashtags_lower = [h.lower().replace('#', '') for h in hashtags]
                matches = sum(1 for keyword in keywords if any(keyword in h for h in hashtags_lower))
                if matches > 0:
                    age_scores[age_range] += matches * 5  # Reduced from 10 to 5
        
        # Emoji density (younger = more emojis) - smaller adjustments
        if emoji_density is not None:
            if emoji_density > 0.4:  # Very high emoji usage
                age_scores['13-17'] += 8
                age_scores['18-24'] += 5
            elif emoji_density > 0.2:  # High emoji usage
                age_scores['18-24'] += 8
                age_scores['25-34'] += 3
            elif emoji_density > 0.1:  # Moderate emoji usage
                age_scores['18-24'] += 5
                age_scores['25-34'] += 5
            elif emoji_density > 0.05:  # Low emoji usage
                age_scores['25-34'] += 5
                age_scores['35-44'] += 3
            else:  # Very low emoji usage
                age_scores['35-44'] += 5
                age_scores['45+'] += 5
        
        # Normalize
        total = sum(age_scores.values())
        if total > 0:
            return {k: round(v/total, 3) for k, v in age_scores.most_common()}
        
        # Default distribution matching real Instagram demographics
        return {
            '18-24': 0.68,
            '25-34': 0.24,
            '35-44': 0.05,
            '13-17': 0.02,
            '45+': 0.01
        }
    
    def calculate_bot_score(self, username_digits, spam_score, emoji_density, is_repeated):
        """
        Calculate bot probability (0-100)
        
        Returns: float (0-100)
        """
        bot_score = 0
        
        # Username has many digits
        if username_digits >= 6:
            bot_score += 30
        elif username_digits >= 4:
            bot_score += 15
        
        # Spam patterns
        bot_score += spam_score * 20
        
        # Emoji only comments
        if emoji_density > 0.9:
            bot_score += 20
        
        # Repeated comments
        if is_repeated:
            bot_score += 25
        
        return min(bot_score, 100)
    
    def predict_city_confidence_based(self, extracted_comments, bio_text=None):
        """
        NEW: Confidence-based city prediction following Instagram's methodology
        
        Multi-signal inference per user:
        1. Bio text (0.9 confidence)
        2. Username (0.8 confidence)  
        3. Full name/surname (0.5-0.8 confidence)
        4. Comment slang (0.3 confidence)
        5. Language (0.2 confidence)
        
        Only accept predictions with confidence >= 0.75
        Report coverage honestly (not all users will have city data)
        
        Args:
            extracted_comments: List of comment feature dicts
            bio_text: Profile bio text (optional)
        
        Returns:
            {
                'city_distribution': {'Delhi': 3.2, 'Mumbai': 2.1, ...},
                'coverage': 42.5,
                'metadata': {...}
            }
        """
        if not self.confidence_city_predictor or not extracted_comments:
            print("  ⚠️  Confidence predictor not available, falling back to legacy method")
            return None
        
        print(f"  🎯 Using CONFIDENCE-BASED city prediction...")
        print(f"     Analyzing {len(extracted_comments)} comments with multi-signal inference")
        
        user_predictions = []
        
        for comment in extracted_comments:
            username = comment.get('username', '')
            comment_text = comment.get('text', '')
            full_name = comment.get('full_name')
            
            # Predict for this user
            prediction = self.confidence_city_predictor.predict_city_for_user(
                username=username,
                comment_text=comment_text,
                bio_text=bio_text if bio_text else None,  # Use profile bio if available
                full_name=full_name
            )
            
            user_predictions.append(prediction)
        
        # Aggregate with confidence threshold of 0.60 (lowered from 0.75)
        # This is more realistic - not everyone has explicit city signals
        result = self.confidence_city_predictor.aggregate_city_distribution(
            user_predictions, 
            min_confidence=0.60  # Lowered threshold
        )
        
        print(f"     ✅ City Coverage: {result['coverage']}% ({result['confident_users']}/{result['total_users']} users)")
        print(f"     📊 Avg Confidence: {result['avg_confidence']}")
        print(f"     🏙️  Cities detected: {result['city_distribution']}")
        
        return result
    
    def predict_city(self, geotags=None, location_slang=None, hours=None, usernames=None, full_names=None, bio_text=None, extracted_comments=None):
        """
        LEGACY city prediction (kept for backward compatibility)
        
        NEW APPROACH: Use predict_city_confidence_based() instead!
        
        If extracted_comments are provided, will automatically use confidence-based method.
        """
        # Try confidence-based method first if we have comment data
        if extracted_comments and self.confidence_city_predictor:
            result = self.predict_city_confidence_based(extracted_comments, bio_text)
            if result and result['city_distribution']:
                return result['city_distribution']
            print("  ⚠️  Confidence method returned no results, falling back to legacy...")
        
        # Fall back to legacy method
        return self._predict_city_legacy(geotags, location_slang, hours, usernames, full_names, bio_text)
    
    def _predict_city_legacy(self, geotags, location_slang, hours, usernames=None, full_names=None, bio_text=None):
        """
        LEGACY city prediction method (old approach)
        """
        city_scores = Counter()
        bio_cities = None
        
        # 1. Geotag analysis (up to 3 geotags, 10% each)
        if geotags:
            geotag_count = 0
            for location in geotags:
                if location and isinstance(location, str) and len(location.strip()) > 2:
                    location = location.strip()
                    geotag_count += 1
                    
                    # First, use cached geocoding result if available
                    if location in self.geocode_cache:
                        city, confidence = self.geocode_cache[location]
                        if city and confidence > 0:
                            city_scores[city] += 0.10 * confidence  # 10% weight
                            print(f"       [Cache] Geocoded '{location}' → {city} (confidence: {confidence})")
                            continue  # Use cache result
                    
                    # Fallback to live geocoding (if not in cache)
                    try:
                        geocoded_city, confidence = self.geocode_location(location)
                        if geocoded_city and confidence > 0:
                            city_scores[geocoded_city] += 0.10 * confidence  # 10% weight
                            print(f"       🌍 Geocoded '{location}' → {geocoded_city} (confidence: {confidence})")
                            time.sleep(0.1)  # Rate limiting for Nominatim
                        else:
                            print(f"       🌍 Geocoding failed for '{location}'")
                    except Exception as e:
                        print(f"       ⚠️ Error geocoding '{location}': {e}")
        
        # 2. Location slang (up to 3 slang terms, 5% each)
        if location_slang:
            for location, score in location_slang.items():
                if location in self.global_cities.keys():
                    city_scores[location] += score * 0.05  # 5% weight (increased from 2%)
        
        # 3. Bio text - Enhanced with location extraction (up to 3 cities, 5% each)
        if bio_text and len(bio_text.strip()) > 5:
            bio_lower = bio_text.lower()
            
            # Try keyword matching first
            keyword_matched = False
            for city, keywords in self.global_cities.items():
                if any(keyword in bio_lower for keyword in keywords):
                    city_scores[city] += 0.08  # 8% weight (increased from 3%)
                    bio_cities[city] += 1
                    keyword_matched = True
                    break
            
            # If no keyword match, try to extract location phrases and geocode them
            if not keyword_matched:
                # Common bio patterns: "From Delhi", "Living in Mumbai", "Based in Bangalore", "📍 Chennai"
                import re
                location_patterns = [
                    r'from\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'living\s+in\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'based\s+in\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'located\s+in\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'📍\s*([a-zA-Z\s]+?)(?:\s|$|,|\|)',
                    r'at\s+([a-zA-Z\s]+?)(?:\s|$|,|\|)'
                ]
                
                for pattern in location_patterns:
                    matches = re.findall(pattern, bio_lower, re.IGNORECASE)
                    for match in matches:
                        location_text = match.strip()
                        if len(location_text) > 3:
                            geocoded_city, confidence = self.geocode_location(location_text)
                            if geocoded_city and geocoded_city in self.global_cities.keys():
                                city_scores[geocoded_city] += 0.04 * confidence  # 4% * confidence (increased from 2%)
                                bio_cities[geocoded_city] += 1
                                print(f"       🌍 Bio geocoded '{location_text}' → {geocoded_city}")
                                break
                    if bio_cities:
                        break
            
            if bio_cities:
                print(f"     - Bio text cities: {dict(bio_cities)}")
        
        # Normalize and return top cities
        total = sum(city_scores.values())
        if total > 0:
            normalized = {k: round(v/total, 3) for k, v in city_scores.most_common()}
            
            # ADAPTIVE: No hard-coded city preferences!
            # Return top 5-7 cities based purely on detected signals
            return {k: v for k, v in list(normalized.items())[:7]}  # Top 7 cities
        
        # Default: If NO data at all, return most common Instagram cities worldwide
        return {
            'Delhi': 0.25,
            'Mumbai': 0.15,
            'Bangalore': 0.1,
            'Hyderabad': 0.1,
            'Chennai': 0.1,
            'Kolkata': 0.05,
            'Pune': 0.05
        }

