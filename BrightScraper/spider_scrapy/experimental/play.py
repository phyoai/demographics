import asyncio
from playwright.async_api import async_playwright


async def scroll_comments_panel(page, times=10, step=350, gap=6000):
    comment_btn = page.get_by_role("button", name="Comment", exact=True)
    await comment_btn.wait_for(state="visible")

    await comment_btn.evaluate(
        """
        async (btn, cfg) => {
            const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

            const findScrollableParent = (node) => {
                let el = node;
                while (el && el !== document.body) {
                    const style = getComputedStyle(el);
                    const canScrollY =
                        (style.overflowY === "auto" || style.overflowY === "scroll") &&
                        el.scrollHeight > el.clientHeight;

                    if (canScrollY) return el;
                    el = el.parentElement;
                }
                return null;
            };

            const rect = btn.getBoundingClientRect();

            // little above the Comment button
            const probeX = rect.left + rect.width / 2;
            const probeY = Math.max(0, rect.top - 120);

            let target = document.elementFromPoint(probeX, probeY);
            let scroller = findScrollableParent(target) || findScrollableParent(btn);

            if (!scroller) {
                throw new Error("Scrollable comments panel not found");
            }

            for (let i = 0; i < cfg.times; i++) {
                scroller.scrollBy({ top: cfg.step, behavior: "smooth" });
                await sleep(cfg.gap);
            }
        }
        """,
        {"times": times, "step": step, "gap": gap},
    )


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        await page.goto("https://www.instagram.com/p/DU8jV5YDMv9/", timeout=5000) # wait for 5 seconds
        await page.get_by_role("button", name="Close").click()
        # await page.get_by_role("button", name="Close").click()
        # await page.get_by_role("button", name="Close").click()

        # call function
        await scroll_comments_panel(page, times=10, step=350, gap=6000)

        await asyncio.sleep(5)
        # await browser.close()


asyncio.run(main())