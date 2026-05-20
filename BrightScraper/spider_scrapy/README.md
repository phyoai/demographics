# Instagram Public Profile Scrapy Project

This project scrapes only publicly visible Instagram profile/post/reel pages without login and stores merged results in `data/profiles.json`.

## What It Collects

### Profile-level fields
- `username`
- `profile_url`
- `full_name`
- `bio`
- `external_url`
- `category`
- `is_verified`
- `followers_text`, `following_text`, `posts_text`
- `followers`, `following`, `posts` (normalized integers when possible)
- `profile_image_url`
- `recent_posts` (up to 12 `/p/` URLs)
- `recent_reels` (up to 12 `/reel/` URLs)
- `scraped_at`

### Post/Reel-level fields
- `shortcode`
- `post_url`
- `post_type` (`post`, `reel`, or `unknown`)
- `caption`
- `published_at`
- `likes_text`, `likes`
- `comments_count_text`, `comments_count`
- `views_text`, `views`
- `thumbnails_or_media_urls`
- `owner_username`
- `comments` (visible comments + replies only)
- `scraped_at`

### Comment fields
- `comment_id` (or `null` if not available)
- `username`
- `text`
- `published_at`
- `likes_text`, `likes`
- `is_pinned`
- `reply_count`
- `owner_replied`
- `replies` (same normalized shape)
- `scraped_at`

## Public-only Rules

- Uses only public Instagram pages.
- No login.
- No cookie theft/session extraction.
- No CAPTCHA bypass, account automation, private endpoint access, or anti-bot bypass tricks.
- Missing or unstable fields are stored as `null` (or `[]` for lists) and scraping continues.

## Setup

1. Create and activate a Python 3.11+ virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Optional Playwright fallback (only if you want JS rendering fallback):

```bash
playwright install chromium
```

## Run

Single username:

```bash
scrapy crawl instagram_profile -a username=therock
```

Multiple usernames:

```bash
scrapy crawl instagram_profile -a usernames=therock,cristiano
```

Enable per-request Playwright fallback:

```bash
set USE_PLAYWRIGHT_FALLBACK=1
scrapy crawl instagram_profile -a username=therock
```

Or per run (without env var):

```bash
scrapy crawl instagram_profile -a username=therock -a use_playwright=true -s USE_PLAYWRIGHT_FALLBACK=1
```

## Run As API (FastAPI)

Start API server:

```bash
uvicorn api:app --host 127.0.0.1 --port 8000 --reload
```

Swagger docs:

```text
http://127.0.0.1:8000/docs
```

Create a scrape job:

```bash
curl -X POST "http://127.0.0.1:8000/scrape" -H "Content-Type: application/json" -d "{\"usernames\":[\"therock\",\"cristiano\"],\"use_playwright\":false}"
```

Check jobs:

```bash
curl "http://127.0.0.1:8000/jobs"
curl "http://127.0.0.1:8000/jobs/<job_id>"
```

Read stored data:

```bash
curl "http://127.0.0.1:8000/profiles"
curl "http://127.0.0.1:8000/profiles/therock"
```

Get analytics (weekly, monthly, yearly):

```bash
curl -X POST "http://127.0.0.1:8000/analytics" -H "Content-Type: application/json" -d "{\"usernames\":[\"therock\",\"cristiano\"]}"
```

Sample analytics response shape:

```json
{
  "generated_at": "2026-04-03T12:40:00+00:00",
  "requested_usernames": ["therock", "cristiano"],
  "found_usernames": ["therock"],
  "not_found_usernames": ["cristiano"],
  "analytics": {
    "therock": {
      "username": "therock",
      "overall": {
        "posts": 12,
        "totals": {
          "likes": 1200000,
          "comments": 43000,
          "views": 5600000,
          "engagement": 6843000
        },
        "averages": {
          "likes": 100000.0,
          "comments": 3583.33,
          "views": 466666.67,
          "engagement_per_post": 570250.0
        }
      },
      "weekly": [],
      "monthly": [],
      "yearly": [],
      "latest_top_post_by_engagement": {
        "window_days": 30,
        "window_start": "2026-03-01",
        "window_end": "2026-03-31",
        "post": {
          "shortcode": "ABC123",
          "post_url": "https://www.instagram.com/p/ABC123/",
          "engagement": 570250
        }
      },
      "top_posts_by_engagement": []
    }
  }
}
```

## Formulas Used

### Scraper Normalization Formulas

- Compact number normalization:
  - `normalized = int(number * multiplier)`
  - `multiplier("")=1`, `multiplier("K")=1_000`, `multiplier("M")=1_000_000`, `multiplier("B")=1_000_000_000`, `multiplier("T")=1_000_000_000_000`
  - Example: `1.2K -> int(1.2 * 1000) = 1200`
- Profile count parsing:
  - `followers = parse_compact_number(followers_text)`
  - `following = parse_compact_number(following_text)`
  - `posts = parse_compact_number(posts_text)`
- Recent URL limits:
  - `recent_posts = unique_preserve_order(discovered_posts)[:MAX_RECENT_POSTS]`
  - `recent_reels = unique_preserve_order(discovered_reels)[:MAX_RECENT_REELS]`
- Comment reply count fallback:
  - If `reply_count` is missing and replies are visible: `reply_count = len(replies)`
- Owner replied inference:
  - `owner_replied = any(reply.username.lower() == owner_username.lower() for reply in replies)`

### Storage / Meta Formulas

- Total profiles:
  - `meta.total_profiles = len(profiles)`
- Total posts:
  - `meta.total_posts = sum(len(profile.posts) for each profile)`
- Job log tail bound:
  - Keep only last `MAX_JOB_LOG_LINES`:
  - `if len(output_tail) > MAX_JOB_LOG_LINES: output_tail = output_tail[-MAX_JOB_LOG_LINES:]`
- Post idempotency key:
  - `post_key = shortcode` (fallback from URL if missing)
- Comment dedupe key:
  - If `comment_id` exists: `key = "id:" + comment_id`
  - Else: `key = "tuple:" + username + "|" + text + "|" + published_at`

### Analytics Formulas

- Effective post date used for time-bucketing:
  - `effective_dt = published_at if parseable else scraped_at`
- Comments used in analytics:
  - `comments = comments_count if comments_count is not null else len(comments_list)`
- Engagement per post:
  - `engagement = (likes or 0) + (comments or 0) + (views or 0)`
- Totals per bucket (overall/weekly/monthly/yearly):
  - `total_likes = sum(likes values where likes is not null)`
  - `total_comments = sum(comment values where comments is not null)`
  - `total_views = sum(view values where views is not null)`
  - `total_engagement = sum(engagement for all posts in bucket)`
- Averages per bucket:
  - `avg_likes = round(total_likes / likes_posts, 2)` where `likes_posts = count(posts with likes)`
  - `avg_comments = round(total_comments / comments_posts, 2)` where `comments_posts = count(posts with comments)`
  - `avg_views = round(total_views / views_posts, 2)` where `views_posts = count(posts with views)`
  - `avg_engagement_per_post = round(total_engagement / posts, 2)`
- Availability counters:
  - `likes_posts = count(posts where likes != null)`
  - `comments_posts = count(posts where comments != null)`
  - `views_posts = count(posts where views != null)`
- Weekly bucket key:
  - `period = "{iso_year}-W{iso_week:02d}"` (ISO week, Monday-Sunday)
- Monthly bucket key:
  - `period = "{year}-{month:02d}"`
- Yearly bucket key:
  - `period = "{year}"`
- Top posts ranking key (`top_posts_by_engagement`):
  - Sort desc by `(engagement, views, likes, comments, effective_dt)`
- Latest top post by engagement (`latest_top_post_by_engagement`):
  - `latest_dt = max(effective_dt across user posts)`
  - `window_start = latest_dt - 30 days`
  - `candidates = posts where effective_dt >= window_start`
  - Choose best candidate by descending `(engagement, views, likes, comments, effective_dt)`

API behavior:
- Uses the same Scrapy spider and writes to `data/profiles.json`.
- Runs one scrape job at a time (`409 Conflict` if another job is active).
- Streams scraper logs while running and keeps a bounded tail in each job (`output_tail`).
- When `use_playwright=true` in `/scrape`, the API sets subprocess env `USE_PLAYWRIGHT_FALLBACK=1` so Playwright handler is enabled correctly.

## Storage and Idempotency

Main storage file: `data/profiles.json`.

- Profile data is stored at `profiles[username].profile`.
- Post/reel data is stored at `profiles[username].posts[shortcode]`.
- Re-runs update existing usernames/posts instead of duplicating.
- Existing data for other usernames is preserved.
- Writes are atomic:
  1. write temp file
  2. `fsync`
  3. replace original

If the JSON file is corrupted, the spider exits with a clear storage error in logs.

## Output Shape

```json
{
  "profiles": {
    "therock": {
      "profile": {
        "username": "therock",
        "profile_url": "https://www.instagram.com/therock/",
        "full_name": "Dwayne Johnson",
        "followers": 393000000,
        "recent_posts": [
          "https://www.instagram.com/p/ABC123/"
        ],
        "recent_reels": [],
        "scraped_at": "2026-04-03T12:00:00+00:00"
      },
      "posts": {
        "ABC123": {
          "shortcode": "ABC123",
          "post_url": "https://www.instagram.com/p/ABC123/",
          "post_type": "post",
          "likes": 123456,
          "comments": [
            {
              "comment_id": "17900000000000000",
              "username": "example_user",
              "text": "Great post",
              "replies": []
            }
          ],
          "scraped_at": "2026-04-03T12:00:03+00:00"
        }
      }
    }
  },
  "meta": {
    "last_run_at": "2026-04-03T12:00:05+00:00",
    "total_profiles": 1,
    "total_posts": 1
  }
}
```

## Reliability Policy

The project enforces conservative request behavior for Instagram:

- `CONCURRENT_REQUESTS = 1`
- `CONCURRENT_REQUESTS_PER_DOMAIN = 1`
- `DOWNLOAD_DELAY = 2`
- `RANDOMIZE_DOWNLOAD_DELAY = False`
- `AUTOTHROTTLE_ENABLED = True`
- `AUTOTHROTTLE_START_DELAY = 2`
- `AUTOTHROTTLE_MAX_DELAY = 10`
- `AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0`
- `RETRY_ENABLED = True`
- `RETRY_TIMES = 3`
- `COOKIES_ENABLED = False`

This guarantees a hard minimum delay of 2 seconds between requests.

If Instagram serves JS-only HTML (no usable post/profile metadata in page source), the spider automatically uses the public, no-login `web_profile_info` JSON endpoint as a fallback to populate profile data and discover recent post/reel URLs.

For visible comment extraction on modern Instagram layouts, run with Playwright fallback enabled (`USE_PLAYWRIGHT_FALLBACK=1`) and optionally `-a use_playwright=true` so JS-rendered comment blocks can be parsed.

When comments are still empty on a post page, the spider also attempts a public JSON post fallback (`?__a=1&__d=dis`) and merges any comment edges/preview comments exposed there.

With Playwright enabled, the spider performs repeated comment expand/scroll cycles (scaled from `MAX_VISIBLE_COMMENTS`) before parsing, then still caps stored comments at `MAX_VISIBLE_COMMENTS`.

## Project Layout

```text
scrapy.cfg
ig_scraper/
  __init__.py
  items.py
  middlewares.py
  pipelines.py
  settings.py
  utils.py
  spiders/
    __init__.py
    instagram_profile.py
data/
  profiles.json
README.md
requirements.txt
```
