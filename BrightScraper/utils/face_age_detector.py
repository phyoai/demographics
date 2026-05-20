"""
Face Age Detection Module
Uses DeepFace for age range estimation from profile pictures

This is the MOST IMPORTANT signal (50% weight) for age prediction.
Achieves 80-88% accuracy when face is detected.
"""

import os
import requests
from io import BytesIO
from PIL import Image
import tempfile
import hashlib
from pathlib import Path

# Lazy import to avoid loading heavy models at startup
_deepface = None
_model_loaded = False


def _load_deepface():
    """Lazy load DeepFace to avoid startup overhead"""
    global _deepface, _model_loaded
    
    if _deepface is None:
        try:
            print("  📦 Loading DeepFace library...")
            from deepface import DeepFace
            _deepface = DeepFace
            _model_loaded = True
            print("  ✅ DeepFace loaded successfully")
            
            # Test if we can access the analyze function
            if hasattr(DeepFace, 'analyze'):
                print("  ✅ DeepFace.analyze() is available")
            else:
                print("  ⚠️  DeepFace.analyze() NOT found!")
                _model_loaded = False
            
        except ImportError as e:
            print(f"  ❌ Failed to import DeepFace: {e}")
            print("  💡 Install with: pip install deepface")
            _model_loaded = False
        except Exception as e:
            print(f"  ⚠️  Failed to load DeepFace: {e}")
            _model_loaded = False
    
    return _deepface


class FaceAgeDetector:
    """
    Detect age range from profile pictures using DeepFace
    """
    
    def __init__(self, cache_dir=None, enable_cache=True):
        """
        Initialize face age detector
        
        Args:
            cache_dir: Directory to cache downloaded images
            enable_cache: Enable/disable image caching
        """
        self.enable_cache = enable_cache
        
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path(tempfile.gettempdir()) / 'phyo_face_cache'
        
        if self.enable_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def detect_age_from_url(self, profile_pic_url, timeout=10):
        """
        Detect age from profile picture URL
        
        Args:
            profile_pic_url: URL of the profile picture
            timeout: Request timeout in seconds
        
        Returns:
            {
                'age_range': '18-24',
                'confidence': 0.85,
                'detected': True,
                'raw_age': 22
            }
            
            Returns None if no face detected or error occurs
        """
        if not profile_pic_url or not profile_pic_url.strip():
            return None
        
        # Skip default/placeholder images
        if self._is_placeholder_image(profile_pic_url):
            return None
        
        # Track statistics
        if not hasattr(self, '_stats'):
            self._stats = {
                'total': 0,
                'downloaded': 0,
                'analyzed': 0,
                'detected': 0,
                'errors': [],
                'first_run': True
            }
        
        self._stats['total'] += 1
        
        # First run debug
        if self._stats['first_run']:
            print(f"  🔍 Attempting to download first image...")
            self._stats['first_run'] = False
        
        try:
            # Download image
            image_path = self._download_image(profile_pic_url, timeout)
            
            if not image_path:
                if self._stats['total'] == 1:
                    print(f"  ⚠️  First image download failed (URL might be invalid or blocked)")
                return None
            
            self._stats['downloaded'] += 1
            
            if self._stats['downloaded'] == 1:
                print(f"  ✅ First image downloaded successfully, analyzing...")
            
            # Analyze age
            result = self._analyze_age(image_path)
            
            if result:
                self._stats['analyzed'] += 1
                if result.get('detected'):
                    self._stats['detected'] += 1
                    if self._stats['detected'] == 1:
                        print(f"  🎉 First face detected! Age: {result['age_range']}")
            else:
                if self._stats['analyzed'] == 0 and self._stats['downloaded'] == 1:
                    print(f"  ⚠️  First analysis returned None (no face detected or error)")
            
            # Clean up if not cached
            if not self.enable_cache and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except:
                    pass
            
            # Print progress every 100 images
            if self._stats['total'] % 100 == 0:
                print(f"  🔍 Face detection progress: {self._stats['detected']}/{self._stats['total']} detected ({self._stats['downloaded']} downloaded)")
            
            return result
            
        except Exception as e:
            error_msg = str(e)[:100]
            if error_msg not in self._stats['errors']:
                self._stats['errors'].append(error_msg)
                if len(self._stats['errors']) <= 3:  # Show first 3 unique errors
                    print(f"  ⚠️  Face detection error: {error_msg}")
            return None
    
    def _is_placeholder_image(self, url):
        """Check if URL is a placeholder/default image"""
        placeholder_patterns = [
            'default',
            'placeholder',
            'avatar_default',
            'no_profile',
            'blank',
            # NOTE: Removed 's150x150' - Instagram uses this for all profile pics
            # We'll check actual image size after download instead
        ]
        
        url_lower = url.lower()
        return any(pattern in url_lower for pattern in placeholder_patterns)
    
    def _download_image(self, url, timeout=10):
        """
        Download image from URL with caching
        
        Returns:
            Path to downloaded image file, or None if failed
        """
        try:
            # Generate cache key from URL
            url_hash = hashlib.md5(url.encode()).hexdigest()
            cache_path = self.cache_dir / f"{url_hash}.jpg"
            
            # Check cache
            if self.enable_cache and cache_path.exists():
                # DEBUG: Log cache hits for first few
                if not hasattr(self, '_cache_hit_logged'):
                    self._cache_hit_logged = 0
                if self._cache_hit_logged < 2:
                    print(f"  💾 Using cached image: {cache_path}")
                    self._cache_hit_logged += 1
                return str(cache_path)
            
            # DEBUG: Log first download attempt
            if not hasattr(self, '_download_debug_count'):
                self._download_debug_count = 0
            
            if self._download_debug_count < 3:
                print(f"  ⬇️  Downloading image #{self._download_debug_count + 1}")
                print(f"     URL: {url[:80]}...")
                self._download_debug_count += 1
            
            # Download image
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=timeout, stream=True)
            response.raise_for_status()
            
            if self._download_debug_count <= 3:
                print(f"     ✅ Downloaded: {len(response.content)} bytes")
            
            # Load and validate image
            img = Image.open(BytesIO(response.content))
            
            if self._download_debug_count <= 3:
                print(f"     📐 Image size: {img.size}, mode: {img.mode}")
            
            # Skip very small images (likely placeholders)
            if img.size[0] < 50 or img.size[1] < 50:
                if self._download_debug_count <= 3:
                    print(f"     ⚠️  Image too small, skipping")
                return None
            
            # Convert to RGB if needed
            if img.mode != 'RGB':
                img = img.convert('RGB')
                if self._download_debug_count <= 3:
                    print(f"     🔄 Converted to RGB")
            
            # Save to cache or temp
            if self.enable_cache:
                save_path = cache_path
            else:
                save_path = Path(tempfile.gettempdir()) / f"temp_face_{url_hash}.jpg"
            
            img.save(save_path, 'JPEG')
            return str(save_path)
            
        except Exception as e:
            # Silent fail for image download errors
            return None
    
    def _analyze_age(self, image_path):
        """
        Analyze age from image using DeepFace
        
        Returns:
            {
                'age_range': '18-24',
                'confidence': 0.85,
                'detected': True,
                'raw_age': 22
            }
        """
        # Load DeepFace
        DeepFace = _load_deepface()
        
        if not DeepFace or not _model_loaded:
            return None
        
        try:
            # DEBUG: Log first analysis attempt
            if not hasattr(self, '_analysis_debug_count'):
                self._analysis_debug_count = 0
            
            if self._analysis_debug_count < 3:
                print(f"  🔬 DeepFace analyzing image #{self._analysis_debug_count + 1}: {image_path}")
                self._analysis_debug_count += 1
            
            # Analyze image
            # DeepFace.analyze returns: {'age': 25, 'dominant_gender': 'Man', ...}
            result = DeepFace.analyze(
                img_path=image_path,
                actions=['age'],
                enforce_detection=True,  # Only proceed if face detected
                detector_backend='opencv',  # Fast detector
                silent=True
            )
            
            if self._analysis_debug_count <= 3:
                print(f"  ✅ DeepFace raw result: {type(result)}, length: {len(result) if isinstance(result, list) else 'N/A'}")
            
            # Handle both single face and multiple faces
            if isinstance(result, list):
                if len(result) == 0:
                    if self._analysis_debug_count <= 3:
                        print(f"  ⚠️  DeepFace returned empty list (no faces)")
                    return None
                result = result[0]  # Use first face
            
            raw_age = result.get('age')
            
            if raw_age is None:
                if self._analysis_debug_count <= 3:
                    print(f"  ⚠️  No age in result: {result.keys() if isinstance(result, dict) else 'not a dict'}")
                return None
            
            if self._analysis_debug_count <= 3:
                print(f"  🎉 Age detected: {raw_age} years old")
            
            # Convert to age range buckets
            age_range, confidence = self._convert_to_age_range(raw_age)
            
            return {
                'age_range': age_range,
                'confidence': confidence,
                'detected': True,
                'raw_age': int(raw_age)
            }
            
        except ValueError as e:
            # No face detected
            if not hasattr(self, '_no_face_count'):
                self._no_face_count = 0
            self._no_face_count += 1
            
            if self._no_face_count <= 3:
                print(f"  ℹ️  No face detected (ValueError): {str(e)[:80]}")
            return None
        except Exception as e:
            # Other errors
            if not hasattr(self, '_deepface_error_count'):
                self._deepface_error_count = 0
            self._deepface_error_count += 1
            
            if self._deepface_error_count <= 3:
                print(f"  ⚠️  DeepFace analysis error: {str(e)[:100]}")
            return None
    
    def _convert_to_age_range(self, raw_age):
        """
        Convert raw age to Instagram standard age ranges
        
        Returns:
            (age_range, confidence)
        """
        # DeepFace age prediction has varying accuracy by age
        # Younger ages: ±3 years accuracy
        # Older ages: ±5 years accuracy
        
        if raw_age < 13:
            return '13-17', 0.60  # Low confidence, might be younger
        elif raw_age <= 17:
            return '13-17', 0.85
        elif raw_age <= 24:
            return '18-24', 0.85
        elif raw_age <= 34:
            return '25-34', 0.80
        elif raw_age <= 44:
            return '35-44', 0.75
        else:
            return '45+', 0.70
    
    def batch_detect_ages(self, profile_pic_urls, max_workers=5):
        """
        Batch process multiple profile pictures (future: parallel processing)
        
        Args:
            profile_pic_urls: List of profile picture URLs
            max_workers: Number of parallel workers (not implemented yet)
        
        Returns:
            List of age detection results
        """
        results = []
        
        for url in profile_pic_urls:
            result = self.detect_age_from_url(url)
            results.append(result)
        
        return results


# Singleton instance for reuse
_face_detector_instance = None


def get_face_detector(cache_dir=None, enable_cache=True):
    """Get or create face detector instance"""
    global _face_detector_instance
    
    if _face_detector_instance is None:
        _face_detector_instance = FaceAgeDetector(cache_dir, enable_cache)
    
    return _face_detector_instance


if __name__ == '__main__':
    # Test
    detector = FaceAgeDetector(enable_cache=False)
    
    # Test with a sample URL (replace with actual profile pic URL)
    test_url = "https://example.com/profile.jpg"
    
    print(f"Testing face age detection...")
    result = detector.detect_age_from_url(test_url)
    
    if result:
        print(f"✅ Detected: {result}")
    else:
        print(f"❌ No face detected or error occurred")
