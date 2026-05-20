"""
AI-Powered Audience Predictor using OpenAI GPT-3.5
Much more accurate than rule-based ML
"""
import os
from openai import OpenAI
import json

class AIAudiencePredictor:
    """Use GPT-3.5 for audience predictions"""
    
    def __init__(self):
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            print("⚠️  OPENAI_API_KEY not found in .env - AI predictions disabled")
            self.client = None
        else:
            self.client = OpenAI(api_key=api_key)
    
    def analyze_commenters_batch(self, commenters, max_batch=20):
        """
        Analyze a batch of commenters using GPT-3.5
        
        Args:
            commenters: List of dicts with 'username' and 'comment' text
            max_batch: Max commenters to analyze in one call (cost control)
        
        Returns:
            Dict with aggregated predictions
        """
        if not self.client:
            return None
        
        # Prepare batch data
        batch = commenters[:max_batch]
        commenters_text = "\n".join([
            f"{i+1}. Username: @{c['username']}, Comment: \"{c['comment'][:100]}\""
            for i, c in enumerate(batch)
        ])
        
        prompt = f"""Analyze these {len(batch)} Instagram commenters and predict their demographics:

{commenters_text}

For each commenter, predict:
1. Gender (male/female/unknown)
2. Age range (13-17, 18-24, 25-34, 35-44, 45+)
3. Country (most likely: India, USA, UK, UAE, Singapore, or other)
4. City (if username/comment suggests specific Indian city: Delhi, Mumbai, Bangalore, Chennai, Hyderabad, Pune)

Focus on:
- Username patterns (Indian names: singh, kumar, sharma, etc.)
- Comment language and slang
- Writing style
- Emoji usage

Return ONLY a JSON object with this structure:
{{
  "predictions": [
    {{"username": "user1", "gender": "male", "age": "18-24", "country": "India", "city": "Delhi"}},
    ...
  ],
  "summary": {{
    "gender_distribution": {{"male": 70, "female": 20, "unknown": 10}},
    "age_distribution": {{"18-24": 60, "25-34": 30, "35-44": 10}},
    "country_distribution": {{"India": 85, "USA": 10, "UK": 5}},
    "city_distribution": {{"Delhi": 3.5, "Mumbai": 2.1, "Bangalore": 1.8}}
  }}
}}

Note: City percentages show actual audience representation (don't normalize to 100%)."""

        try:
            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are an expert at analyzing Instagram user demographics from usernames and comments. Focus on Indian Instagram users."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=1500
            )
            
            result_text = response.choices[0].message.content
            
            # Parse JSON response
            # Remove markdown code blocks if present
            if '```json' in result_text:
                result_text = result_text.split('```json')[1].split('```')[0]
            elif '```' in result_text:
                result_text = result_text.split('```')[1].split('```')[0]
            
            # Clean up the text
            result_text = result_text.strip()
            
            # Try to parse JSON
            try:
                result = json.loads(result_text)
                return result
            except json.JSONDecodeError as je:
                # Try to fix common JSON issues
                print(f"⚠️  JSON parse error, attempting fix... ({str(je)[:50]})")
                
                # Remove trailing commas
                import re
                result_text = re.sub(r',\s*}', '}', result_text)
                result_text = re.sub(r',\s*]', ']', result_text)
                
                try:
                    result = json.loads(result_text)
                    return result
                except:
                    print(f"❌ Could not fix JSON, skipping batch")
                    return None
            
        except Exception as e:
            print(f"❌ GPT-3.5 API error: {e}")
            return None
    
    def analyze_all_commenters(self, commenters):
        """
        Analyze all commenters in batches
        
        Args:
            commenters: List of dicts with 'username' and 'comment'
        
        Returns:
            Aggregated demographics
        """
        if not self.client:
            print("⚠️  OpenAI API not available - using fallback ML")
            return None
        
        print(f"🤖 Using GPT-3.5 to analyze {len(commenters)} commenters...")
        
        # Process in batches of 20
        batch_size = 20
        all_predictions = []
        successful_batches = 0
        failed_batches = 0
        
        for i in range(0, len(commenters), batch_size):
            batch = commenters[i:i+batch_size]
            batch_num = i//batch_size + 1
            total_batches = (len(commenters)-1)//batch_size + 1
            print(f"   Processing batch {batch_num}/{total_batches}...")
            
            result = self.analyze_commenters_batch(batch)
            if result and 'predictions' in result:
                all_predictions.extend(result['predictions'])
                successful_batches += 1
            else:
                failed_batches += 1
                print(f"   ⚠️  Batch {batch_num} failed, continuing...")
        
        print(f"   ✓ Completed: {successful_batches} successful, {failed_batches} failed")
        
        # Aggregate all predictions
        if not all_predictions:
            print("   ❌ No successful predictions, falling back to ML")
            return None
        
        # Calculate final distributions
        from collections import Counter
        
        genders = Counter()
        ages = Counter()
        countries = Counter()
        cities = Counter()
        
        for pred in all_predictions:
            genders[pred.get('gender', 'unknown')] += 1
            ages[pred.get('age', '18-24')] += 1
            countries[pred.get('country', 'India')] += 1
            if pred.get('city'):
                cities[pred['city']] += 1
        
        total = len(all_predictions)
        
        # Calculate representation percentages (not normalized to 100%)
        # This matches Instagram's actual display format
        gender_dist = {k: round(v/total*100, 1) for k, v in genders.items()}
        age_dist = {k: round(v/total*100, 1) for k, v in ages.items()}
        country_dist = {k: round(v/total*100, 1) for k, v in countries.most_common(5)}
        
        # City distribution: Show actual representation (may not sum to 100%)
        # Only show cities with meaningful representation (>0.5%)
        city_dist = {}
        for city, count in cities.most_common(5):
            percentage = round(count/total*100, 1)
            if percentage >= 0.5:  # Only include cities with at least 0.5% representation
                city_dist[city] = percentage
        
        return {
            'gender_distribution': gender_dist,
            'age_distribution': age_dist,
            'country_distribution': country_dist,
            'city_distribution': city_dist,
            'total_analyzed': total
        }
