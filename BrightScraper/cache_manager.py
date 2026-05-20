"""
Caching system for API responses.
DISABLED by default to save storage - can be enabled via environment variable.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta


CACHE_DIR = 'api_cache'
CACHE_VERSION = '2.1'  # Increment when format changes

# Disable caching by default to save storage
ENABLE_CACHING = os.getenv('ENABLE_API_CACHING', 'false').lower() == 'true'

# Cache older than this many days is treated as stale (default: 7 days)
CACHE_MAX_AGE_DAYS = int(os.getenv('CACHE_MAX_AGE_DAYS', '7'))


def ensure_cache_dir():
    """Create cache directory if it does not exist and caching is enabled."""
    if not ENABLE_CACHING:
        return

    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
        print(f'Created cache directory: {CACHE_DIR}')


def get_cache_key(data_type, identifier):
    """
    Generate cache key.

    Args:
        data_type: 'profile', 'comments', etc.
        identifier: username or post_url
    """
    key = f"{data_type}_{identifier}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def is_cache_expired(cache_data):
    """
    Check whether cache entry is older than configured max age.

    Missing/invalid timestamps are treated as expired to avoid stale reuse.
    """
    cached_at_raw = cache_data.get('cached_at')
    if not cached_at_raw:
        return True

    try:
        cached_at = datetime.fromisoformat(cached_at_raw)
    except Exception:
        return True

    max_age_days = max(CACHE_MAX_AGE_DAYS, 0)
    max_age = timedelta(days=max_age_days)
    return (datetime.now() - cached_at) > max_age


def save_to_cache(data_type, identifier, data):
    """
    Save data to cache.

    Args:
        data_type: 'profile', 'comments'
        identifier: username or post_url
        data: JSON-serializable data to save
    """
    if not ENABLE_CACHING:
        return None

    ensure_cache_dir()

    cache_key = get_cache_key(data_type, identifier)
    filename = f"{data_type}_{cache_key}.json"
    filepath = os.path.join(CACHE_DIR, filename)

    cache_data = {
        'cache_version': CACHE_VERSION,
        'cached_at': datetime.now().isoformat(),
        'data_type': data_type,
        'identifier': identifier,
        'data': data,
    }

    with open(filepath, 'w', encoding='utf-8') as file_obj:
        json.dump(cache_data, file_obj, indent=2, ensure_ascii=False)

    print(f'Cached: {data_type} for {identifier}')
    return filepath


def load_from_cache(data_type, identifier):
    """
    Load data from cache.

    Returns:
        Cached data or None if not found, stale, version mismatch, or disabled.
    """
    if not ENABLE_CACHING:
        return None

    ensure_cache_dir()

    cache_key = get_cache_key(data_type, identifier)
    filename = f"{data_type}_{cache_key}.json"
    filepath = os.path.join(CACHE_DIR, filename)

    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, 'r', encoding='utf-8') as file_obj:
            cache_data = json.load(file_obj)

        # Version check (legacy behavior retained for comments)
        cached_version = cache_data.get('cache_version', '1.0')
        if data_type == 'comments' and cached_version != CACHE_VERSION:
            print(f'Old cache version ({cached_version}) for {data_type}, fetching fresh data...')
            os.remove(filepath)
            return None

        # Age check (new behavior): expire entries older than configured max age
        if is_cache_expired(cache_data):
            print(f'Cache expired for {data_type}:{identifier}, fetching fresh data...')
            os.remove(filepath)
            return None

        print(f'Loaded from cache: {data_type} for {identifier}')
        return cache_data['data']
    except Exception as exc:
        print(f'Cache read error: {exc}')
        return None


def clear_cache():
    """Clear all cached data."""
    ensure_cache_dir()

    files = os.listdir(CACHE_DIR)
    for filename in files:
        if filename.endswith('.json'):
            os.remove(os.path.join(CACHE_DIR, filename))

    print(f'Cleared {len(files)} cached files')


def clear_old_comment_caches():
    """Clear old version comment caches (BrightData format)."""
    ensure_cache_dir()

    cleared_count = 0
    files = os.listdir(CACHE_DIR)

    for filename in files:
        if filename.startswith('comments_') and filename.endswith('.json'):
            filepath = os.path.join(CACHE_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as file_obj:
                    cache_data = json.load(file_obj)

                cached_version = cache_data.get('cache_version', '1.0')
                if cached_version != CACHE_VERSION:
                    os.remove(filepath)
                    cleared_count += 1
                    print(f'Removed old cache: {filename}')
            except Exception:
                os.remove(filepath)
                cleared_count += 1

    print(f'Cleared {cleared_count} old comment cache files')
    return cleared_count


def list_cache():
    """List all cached data."""
    ensure_cache_dir()

    files = os.listdir(CACHE_DIR)
    print(f'\nCached files ({len(files)}):')

    for filename in files:
        if filename.endswith('.json'):
            filepath = os.path.join(CACHE_DIR, filename)
            with open(filepath, 'r', encoding='utf-8') as file_obj:
                cache_data = json.load(file_obj)
            print(
                f'  - {cache_data["data_type"]}: {cache_data["identifier"]} '
                f'(cached at {cache_data["cached_at"]})'
            )
