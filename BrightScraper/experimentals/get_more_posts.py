from playwright.sync_api import sync_playwright
import random
from urllib.parse import urlparse, unquote

TARGET_PAGE = "https://www.instagram.com/thewordmuse_/"
BLOCK_TEXT = "See everyday moments from your close friends."

proxies_list = [
    "http://vkgkgitp:k7jv2pmjwu0s@31.59.20.176:6754",
    "http://vkgkgitp:k7jv2pmjwu0s@23.95.150.145:6114",
    "http://vkgkgitp:k7jv2pmjwu0s@198.23.239.134:6540",
    "http://vkgkgitp:k7jv2pmjwu0s@45.38.107.97:6014",
    "http://vkgkgitp:k7jv2pmjwu0s@107.172.163.27:6543",
    "http://vkgkgitp:k7jv2pmjwu0s@198.105.121.200:6462",
    "http://vkgkgitp:k7jv2pmjwu0s@216.10.27.159:6837",
    "http://vkgkgitp:k7jv2pmjwu0s@142.111.67.146:5611",
    "http://vkgkgitp:k7jv2pmjwu0s@191.96.254.138:6185",
    "http://vkgkgitp:k7jv2pmjwu0s@31.58.9.4:6077",
]

def build_playwright_proxy(proxy_url: str):
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port or 80}"

    cfg = {"server": server}
    if parsed.username:
        cfg["username"] = unquote(parsed.username)
    if parsed.password:
        cfg["password"] = unquote(parsed.password)

    return cfg


def is_proxy_working(proxy_cfg, test_url="https://www.instagram.com/", timeout=15000):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy_cfg)
            context = browser.new_context()
            page = context.new_page()
            page.goto(test_url, wait_until="domcontentloaded", timeout=timeout)

            current_url = page.url
            title = page.title()

            browser.close()

            print(f"[OK] Proxy working: {proxy_cfg['server']} | URL: {current_url} | Title: {title}")
            return True

    except Exception as e:
        print(f"[FAIL] Proxy failed: {proxy_cfg['server']} | Error: {e}")
        return False


def get_working_proxies(proxy_urls):
    working = []
    for proxy_url in proxy_urls:
        proxy_cfg = build_playwright_proxy(proxy_url)
        # if not proxy_cfg:
        #     continue
        # if is_proxy_working(proxy_cfg):
        working.append(proxy_cfg)
    return working


def page_has_block_text(page, text: str = BLOCK_TEXT) -> bool:
    try:
        body_text = page.text_content("body", timeout=5000) or ""
        return text in body_text or "something went wrong" in body_text.lower()
    except Exception:
        return False


def main():
    working_proxies = get_working_proxies(proxies_list)
    if not working_proxies:
        print("No working proxies found.")
        return

    print(f"\nTotal working proxies: {len(working_proxies)}")

    # Try proxies in random order until the block text is NOT seen
    tried = 0
    with sync_playwright() as p:
        for chosen_proxy in random.sample(working_proxies, len(working_proxies)):
            tried += 1
            print(f"Attempt {tried}: Using proxy: {chosen_proxy['server']}")

            browser = p.chromium.launch(headless=False, proxy=chosen_proxy)
            context = browser.new_context()
            page = context.new_page()

            try:
                page.goto(TARGET_PAGE, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)  # let any overlays render

                if page_has_block_text(page, BLOCK_TEXT):
                    print(f"Block text detected. Switching proxy... ({chosen_proxy['server']})")
                    browser.close()
                    continue

                print("Instagram page opened successfully without block text.")
                # Keep the browser open for manual inspection; comment out next line if needed
                # page.wait_for_timeout(15000)
                return

            except Exception as e:
                print(f"Error loading target with {chosen_proxy['server']}: {e}")
            finally:
                # Close this attempt's browser before trying next proxy
                try:
                    browser.close()
                except Exception:
                    pass

    print("Exhausted all working proxies, but the block text persisted or errors occurred.")


if __name__ == "__main__":
    main()