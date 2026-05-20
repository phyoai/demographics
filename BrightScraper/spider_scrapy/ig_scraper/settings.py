import importlib.util
import os


BOT_NAME = "ig_scraper"

SPIDER_MODULES = ["ig_scraper.spiders"]
NEWSPIDER_MODULE = "ig_scraper.spiders"

ROBOTSTXT_OBEY = False

CONCURRENT_REQUESTS = 4
CONCURRENT_REQUESTS_PER_DOMAIN = 4
DOWNLOAD_DELAY = 1
RANDOMIZE_DOWNLOAD_DELAY = True

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 5
AUTOTHROTTLE_MAX_DELAY = 10
AUTOTHROTTLE_TARGET_CONCURRENCY = 3.0

RETRY_ENABLED = True
RETRY_TIMES = 2
COOKIES_ENABLED = False
FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = "INFO"

DOWNLOAD_TIMEOUT = 30
TELNETCONSOLE_ENABLED = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

ITEM_PIPELINES = {
    "ig_scraper.pipelines.InstagramJsonStoragePipeline": 300,
}

DOWNLOADER_MIDDLEWARES = {
    "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
    "ig_scraper.middlewares.InstagramRetryMiddleware": 550,
}

OUTPUT_JSON_PATH = "data/profiles.json"
MAX_VISIBLE_COMMENTS = 30
MAX_RECENT_POSTS = 20
MAX_RECENT_REELS = 20

PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("scrapy_playwright") is not None
USE_PLAYWRIGHT_FALLBACK = os.getenv("USE_PLAYWRIGHT_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_LAUNCH_OPTIONS = {"headless": True}
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 30_000

PLAYWRIGHT_HANDLER_ENABLED = bool(USE_PLAYWRIGHT_FALLBACK and PLAYWRIGHT_AVAILABLE)
if PLAYWRIGHT_HANDLER_ENABLED:
    TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
    DOWNLOAD_HANDLERS = {
        "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    }
