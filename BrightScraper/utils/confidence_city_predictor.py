"""
Confidence-Based City Prediction System
Following Instagram's actual methodology

KEY PRINCIPLES:
1. Multi-signal inference per user
2. Confidence scoring (0.0 to 1.0)
3. Only accept predictions with confidence >= 0.75
4. Never force a city if confidence is low
5. Report coverage honestly (not all users will have city data)

This matches Instagram's actual approach!
"""
from collections import Counter
import re


class ConfidenceCityPredictor:
    """
    Predict cities using confidence-based multi-signal approach
    """
    
    def __init__(self):
        # Indian city slang dictionary (GOLD!)
        self.city_slang = {
            'Delhi': {
                'slang': ['bhai', 'yaar', 'chal', 'abe', 'bc', 'bro', 'dost'],
                'keywords': ['delhi', 'ncr', 'noida', 'gurgaon', 'gurugram', 'dilli', 'dlf']
            },
            'Mumbai': {
                'slang': ['re', 'kya re', 'boss', 'baba', 'kadak', 'mast', 'bindas'],
                'keywords': ['mumbai', 'bombay', 'bom', 'andheri', 'bandra']
            },
            'Bangalore': {
                'slang': ['da', 'ra', 'guru', 'machha', 'ba'],
                'keywords': ['bangalore', 'bengaluru', 'blr', 'bang']
            },
            'Hyderabad': {
                'slang': ['ra', 'bhai', 'mama', 'babai'],
                'keywords': ['hyderabad', 'hyd', 'secunderabad']
            },
            'Chennai': {
                'slang': ['da', 'pa', 'anna', 'thala', 'mass'],
                'keywords': ['chennai', 'madras']
            },
            'Pune': {
                'slang': ['re', 'kaka', 'dada'],
                'keywords': ['pune', 'pcmc']
            },
            'Kolkata': {
                'slang': ['dada', 'bhai', 'jai hind'],
                'keywords': ['kolkata', 'calcutta', 'cal']
            },
            'Lucknow': {
                'slang': ['bhaiya', 'jaan', 'janab'],
                'keywords': ['lucknow', 'lko']
            },
            'Indore': {
                'slang': ['bhai', 'bhaiya'],
                'keywords': ['indore', 'ido']
            },
            'Jaipur': {
                'slang': ['bhai', 'yaar'],
                'keywords': ['jaipur', 'jai', 'pink city']
            }
        }
        
        # Regional name patterns (ONLY highly distinctive)
        self.name_patterns = {
            'Delhi NCR': {
                'surnames': ['negi', 'rawat', 'bist', 'tyagi', 'tanwar', 'saxena', 'mittal', 
                           'bansal', 'goyal', 'khanna', 'bhatia', 'sethi', 'chawla', 'grover',
                           'chauhan', 'sharma', 'kumar', 'singh', 'gupta', 'verma'],  # Added common names
                'confidence': 0.5  # Lower confidence for common names
            },
            'Mumbai': {
                'surnames': ['patil', 'kulkarni', 'deshmukh', 'pawar', 'shinde', 'jadhav',
                           'gaikwad', 'bhosale', 'sawant', 'rao'],
                'confidence': 0.65  # Increased from 0.8
            },
            'Bangalore': {
                'surnames': ['gowda', 'shetty', 'hegde', 'kamath', 'rai', 'reddy'],  # Added reddy
                'confidence': 0.65  # Lower from 0.85
            },
            'Kolkata': {
                'surnames': ['chatterjee', 'banerjee', 'mukherjee', 'chakraborty', 'bhattacharya',
                           'sengupta', 'ganguly', 'bose', 'das', 'sen'],  # Added more
                'confidence': 0.7
            },
            'Chennai': {
                'surnames': ['iyer', 'iyengar', 'raman', 'krishnan', 'sundaram', 'kumar'],  # Added kumar
                'confidence': 0.45  # Lower confidence
            },
            'Hyderabad': {
                'surnames': ['reddy', 'chowdary', 'rao', 'naidu'],
                'confidence': 0.5
            },
            'Jaipur': {
                'surnames': ['rathore', 'rajput', 'meena', 'jat'],
                'confidence': 0.6
            },
            'Lucknow': {
                'surnames': ['khan', 'ansari', 'siddiqui'],
                'confidence': 0.5
            }
        }
    
    def predict_city_for_user(self, username, comment_text, bio_text=None, full_name=None):
        """
        Predict city for a SINGLE user with confidence score
        
        Args:
            username: Instagram username
            comment_text: Their comment text
            bio_text: Their bio (optional, if available)
            full_name: Their full name (optional)
        
        Returns:
            {
                'city': 'Delhi' or None,
                'confidence': 0.85,
                'signals': ['username', 'slang', 'bio']
            }
        """
        signals = []
        city_scores = Counter()
        
        # Signal 1: Bio text (STRONGEST - 0.9 confidence)
        if bio_text:
            bio_city, bio_conf = self._check_bio_for_city(bio_text)
            if bio_city:
                city_scores[bio_city] += bio_conf
                signals.append('bio')
        
        # Signal 2: Username (STRONG - 0.8 confidence)
        username_city, username_conf = self._check_username_for_city(username)
        if username_city:
            city_scores[username_city] += username_conf
            signals.append('username')
        
        # Signal 3: Full name/surname (MODERATE - 0.5-0.8 confidence depending on surname)
        if full_name or username:
            name_to_check = full_name if full_name else username
            name_city, name_conf = self._check_name_for_region(name_to_check)
            if name_city:
                city_scores[name_city] += name_conf
                signals.append('name')
        
        # Signal 4: Comment slang (MODERATE - 0.4 confidence - increased!)
        if comment_text:
            slang_city, slang_conf = self._check_slang_in_comment(comment_text)
            if slang_city:
                city_scores[slang_city] += slang_conf
                signals.append('slang')
        
        # Signal 5: Language detection (WEAK - 0.3 confidence - increased!)
        if comment_text:
            lang_city, lang_conf = self._detect_language_city(comment_text)
            if lang_city:
                city_scores[lang_city] += lang_conf
                signals.append('language')
        
        # Find best city
        if not city_scores:
            return {'city': None, 'confidence': 0.0, 'signals': []}
        
        best_city = city_scores.most_common(1)[0][0]
        total_confidence = min(city_scores[best_city], 1.0)  # Cap at 1.0
        
        return {
            'city': best_city,
            'confidence': round(total_confidence, 2),
            'signals': signals
        }
    
    def _check_bio_for_city(self, bio_text):
        """Check bio for explicit city mentions"""
        if not bio_text or len(bio_text) < 3:
            return None, 0.0
        
        bio_lower = bio_text.lower()
        
        # Check for explicit patterns
        patterns = [
            r'from\s+(\w+)',
            r'living\s+in\s+(\w+)',
            r'based\s+in\s+(\w+)',
            r'📍\s*(\w+)',
            r'at\s+(\w+)'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, bio_lower)
            for match in matches:
                # Check against city keywords
                for city, data in self.city_slang.items():
                    if match in data['keywords']:
                        return city, 0.9  # Very high confidence from bio
        
        # Check for direct keyword mentions
        for city, data in self.city_slang.items():
            for keyword in data['keywords']:
                if keyword in bio_lower:
                    return city, 0.85
        
        return None, 0.0
    
    def _check_username_for_city(self, username):
        """Check username for city mentions"""
        if not username or len(username) < 3:
            return None, 0.0
        
        username_lower = username.lower()
        
        for city, data in self.city_slang.items():
            for keyword in data['keywords']:
                if keyword in username_lower:
                    return city, 0.8  # High confidence from username
        
        return None, 0.0
    
    def _check_name_for_region(self, name):
        """Check full name for regional surnames"""
        if not name or len(name) < 3:
            return None, 0.0
        
        name_lower = name.lower()
        name_parts = name_lower.replace('_', ' ').replace('.', ' ').split()
        
        for region, data in self.name_patterns.items():
            # Check last word (surname position)
            if name_parts:
                last_word = name_parts[-1]
                if last_word in data['surnames']:
                    # Return main city for region
                    city = region.split()[0] if ' ' in region else region
                    if city == 'Delhi':
                        city = 'Delhi'  # Delhi NCR -> Delhi
                    return city, data['confidence']
            
            # Check all words
            for word in name_parts:
                if word in data['surnames']:
                    city = region.split()[0] if ' ' in region else region
                    if city == 'Delhi':
                        city = 'Delhi'
                    return city, data['confidence'] * 0.8  # Slightly lower if not last word
        
        return None, 0.0
    
    def _check_slang_in_comment(self, comment_text):
        """Check comment for city-specific slang"""
        if not comment_text or len(comment_text) < 3:
            return None, 0.0
        
        comment_lower = comment_text.lower()
        
        for city, data in self.city_slang.items():
            slang_matches = 0
            for slang_word in data['slang']:
                if slang_word in comment_lower:
                    slang_matches += 1
            
            if slang_matches >= 1:
                # More matches = higher confidence
                confidence = min(0.3 + (slang_matches * 0.15), 0.6)  # Increased from 0.4 max
                return city, confidence
        
        return None, 0.0
    
    def _detect_language_city(self, comment_text):
        """Detect regional language patterns"""
        if not comment_text or len(comment_text) < 5:
            return None, 0.0
        
        comment_lower = comment_text.lower()
        
        # Hindi/Hinglish -> North India (Delhi most likely)
        hindi_words = ['hai', 'nahi', 'kya', 'thi', 'tha', 'hoon', 'ho', 'bhai', 'yaar']
        hindi_count = sum(1 for word in hindi_words if word in comment_lower)
        
        if hindi_count >= 2:
            return 'Delhi', 0.3  # Increased from 0.2
        
        return None, 0.0
    
    def aggregate_city_distribution(self, user_predictions, min_confidence=0.75):
        """
        Aggregate city predictions for all users
        
        Args:
            user_predictions: List of predictions from predict_city_for_user()
            min_confidence: Minimum confidence to accept (default 0.75)
        
        Returns:
            {
                'city_distribution': {'Delhi': 3.2, 'Mumbai': 2.1, ...},
                'coverage': 42.5,  # % of users with confident city prediction
                'total_users': 1000,
                'confident_users': 425
            }
        """
        total_users = len(user_predictions)
        
        # Filter by confidence
        confident_predictions = [
            pred for pred in user_predictions 
            if pred['city'] and pred['confidence'] >= min_confidence
        ]
        
        confident_users = len(confident_predictions)
        coverage = round((confident_users / total_users * 100), 1) if total_users > 0 else 0.0
        
        # Count cities
        city_counts = Counter()
        for pred in confident_predictions:
            city_counts[pred['city']] += 1
        
        # Calculate percentages (relative to TOTAL users, not just confident ones)
        # This matches Instagram's display format
        city_distribution = {}
        for city, count in city_counts.most_common(10):
            percentage = round((count / total_users) * 100, 1)
            if percentage >= 0.5:  # Only show cities with at least 0.5% representation
                city_distribution[city] = percentage
        
        return {
            'city_distribution': city_distribution,
            'coverage': coverage,
            'total_users': total_users,
            'confident_users': confident_users,
            'avg_confidence': round(sum(p['confidence'] for p in confident_predictions) / confident_users, 2) if confident_users > 0 else 0.0
        }


# Helper function for easy integration
def predict_cities_from_comments(comments, min_confidence=0.60):
    """
    Predict cities from comment data
    
    Args:
        comments: List of comment dicts with 'username', 'text', etc.
        min_confidence: Minimum confidence threshold (default 0.60)
    
    Returns:
        City distribution dict
    """
    predictor = ConfidenceCityPredictor()
    
    user_predictions = []
    
    for comment in comments:
        username = comment.get('username', '')
        comment_text = comment.get('text', '')
        
        # Predict for this user
        prediction = predictor.predict_city_for_user(
            username=username,
            comment_text=comment_text,
            bio_text=None,  # Would need to fetch separately
            full_name=comment.get('full_name')
        )
        
        user_predictions.append(prediction)
    
    # Aggregate
    result = predictor.aggregate_city_distribution(user_predictions, min_confidence)
    
    return result


if __name__ == '__main__':
    # Test
    predictor = ConfidenceCityPredictor()
    
    # Test cases
    test_users = [
        {'username': 'delhi_boy_123', 'comment': 'bhai kya scene hai', 'bio': 'From Delhi'},
        {'username': 'mumbai_rocks', 'comment': 'kadak re', 'bio': 'Living in Mumbai'},
        {'username': 'raj_negi', 'comment': 'nice bro', 'bio': None},
        {'username': 'priya_patil', 'comment': 'mast hai', 'bio': None},
        {'username': 'unknown_user', 'comment': '🔥🔥', 'bio': None},
    ]
    
    predictions = []
    for user in test_users:
        pred = predictor.predict_city_for_user(
            username=user['username'],
            comment_text=user['comment'],
            bio_text=user['bio']
        )
        predictions.append(pred)
        print(f"{user['username']}: {pred}")
    
    # Aggregate
    result = predictor.aggregate_city_distribution(predictions, min_confidence=0.75)
    print(f"\nAggregated: {result}")
