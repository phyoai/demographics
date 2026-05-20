from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from experimental.html_parse import parse_html_file
except Exception:  # pragma: no cover
    from html_parse import parse_html_file

from ig_scraper.utils import atomic_write_json, extract_shortcode_from_url, normalize_text, utc_now_iso


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "insta_dump"
DEFAULT_PROFILES_JSON = PROJECT_ROOT / "data" / "profiles.json"
DEFAULT_CONCURRENCY = 5
DEFAULT_SCROLL_TIMES = 15
DEFAULT_SCROLL_STEP = 350
DEFAULT_MIN_SCROLL_GAP_MS = 3000
DEFAULT_MAX_SCROLL_GAP_MS = 10000
DEFAULT_TIMEOUT_MS = 45000


def _emit_progress(on_progress: Callable[[str], None] | None, message: str) -> None:
    if on_progress is None:
        return
    try:
        on_progress(message)
    except Exception:
        return


def default_storage() -> dict[str, Any]:
    return {
        "profiles": {},
        "meta": {
            "last_run_at": None,
            "total_profiles": 0,
            "total_posts": 0,
        },
    }


def load_storage(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_storage()

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Storage JSON is corrupted at {path}. Fix or remove this file before running this endpoint."
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError("Storage root must be a JSON object.")
    if not isinstance(payload.get("profiles"), dict):
        raise ValueError("Storage field 'profiles' must be a JSON object.")
    if not isinstance(payload.get("meta"), dict):
        raise ValueError("Storage field 'meta' must be a JSON object.")
    return payload


def normalize_post_url(url: str) -> str:
    text = normalize_text(url) or ""
    if not text:
        return text
    if text.startswith("//"):
        text = "https:" + text
    if not text.startswith("http://") and not text.startswith("https://"):
        text = "https://www.instagram.com/" + text.lstrip("/")
    return text.split("?", 1)[0]


def normalize_post_urls(post_urls: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in post_urls:
        normalized = normalize_post_url(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def merge_parsed_result_into_storage(
    storage: dict[str, Any],
    parsed_result: dict[str, Any],
    fallback_post_url: str | None = None,
) -> bool:
    post_meta = parsed_result.get("post")
    if not isinstance(post_meta, dict):
        return False

    owner_username = normalize_text(post_meta.get("owner_username"))
    if not owner_username:
        return False
    owner_username = owner_username.lower()

    canonical_url = normalize_text(post_meta.get("canonical_url")) or normalize_post_url(fallback_post_url or "")
    if not canonical_url:
        return False

    shortcode = extract_shortcode_from_url(canonical_url)
    if not shortcode:
        return False

    profiles = storage.get("profiles", {})
    if not isinstance(profiles, dict):
        return False

    profile_bundle = profiles.get(owner_username)
    if not isinstance(profile_bundle, dict):
        return False

    posts = profile_bundle.get("posts")
    if not isinstance(posts, dict):
        return False

    post = posts.get(shortcode)
    if not isinstance(post, dict):
        return False

    parsed_comments = parsed_result.get("comments")
    if isinstance(parsed_comments, list):
        post["comments"] = parsed_comments
    else:
        post["comments"] = []
    post["comment_insights"] = parsed_result.get("insights", {})
    return True


def update_meta(storage: dict[str, Any]) -> None:
    profiles = storage.setdefault("profiles", {})
    meta = storage.setdefault("meta", {})
    total_posts = 0
    for bundle in profiles.values():
        if not isinstance(bundle, dict):
            continue
        posts = bundle.get("posts")
        if isinstance(posts, dict):
            total_posts += len(posts)
    meta["last_run_at"] = utc_now_iso()
    meta["total_profiles"] = len(profiles)
    meta["total_posts"] = total_posts


async def close_popup_if_present(page) -> None:
    close_btn = page.get_by_role("button", name="Close")
    try:
        await close_btn.first.click(timeout=3000)
        await page.wait_for_timeout(1000)
    except Exception:
        return


async def scroll_comments_panel(
    page,
    times: int = DEFAULT_SCROLL_TIMES,
    step: int = DEFAULT_SCROLL_STEP,
    min_gap_ms: int = DEFAULT_MIN_SCROLL_GAP_MS,
    max_gap_ms: int = DEFAULT_MAX_SCROLL_GAP_MS,
) -> None:
    comment_btn = page.get_by_role("button", name="Comment", exact=True).first
    await comment_btn.wait_for(state="visible", timeout=15000)

    await comment_btn.evaluate(
        """
        async (btn, cfg) => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const randInt = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;

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
            const probeX = rect.left + rect.width / 2;
            const probeY = Math.max(0, rect.top - 120);
            const target = document.elementFromPoint(probeX, probeY);
            const scroller = findScrollableParent(target) || findScrollableParent(btn);
            if (!scroller) return;

            for (let i = 0; i < cfg.times; i++) {
                scroller.scrollBy({ top: cfg.step, behavior: "smooth" });
                await sleep(randInt(cfg.min_gap_ms, cfg.max_gap_ms));
            }
        }
        """,
        {
            "times": times,
            "step": step,
            "min_gap_ms": min_gap_ms,
            "max_gap_ms": max_gap_ms,
        },
    )


async def save_html_and_text(page, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "page.html"
    text_path = output_dir / "page.txt"
    screenshot_path = output_dir / "page.png"

    html_content = await page.content()
    html_path.write_text(html_content, encoding="utf-8")

    body_text = await page.locator("body").inner_text()
    text_path.write_text(body_text, encoding="utf-8")

    await page.screenshot(path=str(screenshot_path), full_page=True)
    return {
        "html": html_path,
        "text": text_path,
        "screenshot": screenshot_path,
    }


async def capture_single_post(
    browser,
    semaphore: asyncio.Semaphore,
    post_url: str,
    output_root: Path,
    scroll_times: int,
    scroll_step: int,
    min_scroll_gap_ms: int,
    max_scroll_gap_ms: int,
    timeout_ms: int,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    async with semaphore:
        normalized_url = normalize_post_url(post_url)
        _emit_progress(on_progress, f"Capture started for {normalized_url}")
        shortcode = extract_shortcode_from_url(normalized_url) or "post"
        run_id = uuid.uuid4().hex[:8]
        output_dir = output_root / f"{shortcode}_{run_id}"

        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        try:
            await page.goto(normalized_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(4000)
            await close_popup_if_present(page)
            await scroll_comments_panel(
                page,
                times=scroll_times,
                step=scroll_step,
                min_gap_ms=min_scroll_gap_ms,
                max_gap_ms=max_scroll_gap_ms,
            )
            await page.wait_for_timeout(3000)

            paths = await save_html_and_text(page, output_dir=output_dir)
            return {
                "post_url": normalized_url,
                "ok": True,
                "dump_dir": str(output_dir),
                "html_path": str(paths["html"]),
                "text_path": str(paths["text"]),
                "screenshot_path": str(paths["screenshot"]),
                "error": None,
            }
        except PlaywrightTimeoutError as exc:
            _emit_progress(on_progress, f"Capture timeout for {normalized_url}: {exc}")
            return {
                "post_url": normalized_url,
                "ok": False,
                "dump_dir": str(output_dir),
                "html_path": None,
                "text_path": None,
                "screenshot_path": None,
                "error": f"Timeout: {exc}",
            }
        except Exception as exc:
            _emit_progress(on_progress, f"Capture failed for {normalized_url}: {exc!r}")
            return {
                "post_url": normalized_url,
                "ok": False,
                "dump_dir": str(output_dir),
                "html_path": None,
                "text_path": None,
                "screenshot_path": None,
                "error": repr(exc),
            }
        finally:
            await context.close()


async def capture_posts_parallel(
    post_urls: list[str],
    output_root: Path,
    concurrency: int,
    scroll_times: int,
    scroll_step: int,
    min_scroll_gap_ms: int,
    max_scroll_gap_ms: int,
    timeout_ms: int,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    if not post_urls:
        return []

    semaphore = asyncio.Semaphore(max(1, concurrency))
    _emit_progress(on_progress, f"Capture queue prepared: total_urls={len(post_urls)} concurrency={max(1, concurrency)}.")
    async with async_playwright() as playwright:
        # Headless mode is always enforced for this API flow.
        browser = await playwright.chromium.launch(headless=True)
        try:
            tasks = [
                asyncio.create_task(
                    capture_single_post(
                        browser=browser,
                        semaphore=semaphore,
                        post_url=url,
                        output_root=output_root,
                        scroll_times=scroll_times,
                        scroll_step=scroll_step,
                        min_scroll_gap_ms=min_scroll_gap_ms,
                        max_scroll_gap_ms=max_scroll_gap_ms,
                        timeout_ms=timeout_ms,
                        on_progress=on_progress,
                    )
                )
                for url in post_urls
            ]
            results: list[dict[str, Any]] = []
            completed = 0
            total = len(tasks)
            for done in asyncio.as_completed(tasks):
                result = await done
                results.append(result)
                completed += 1
                status_text = "ok" if result.get("ok") else "failed"
                _emit_progress(
                    on_progress,
                    (
                        f"Capture completed {completed}/{total}: "
                        f"{result.get('post_url')} status={status_text}"
                    ),
                )
            return results
        finally:
            await browser.close()


def merge_parsed_results(
    storage_path: Path,
    parsed_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    storage = load_storage(storage_path)
    merged_count = 0
    skipped_count = 0

    for payload in parsed_payloads:
        parsed_result = payload.get("parsed_result")
        fallback_post_url = payload.get("post_url")
        if not isinstance(parsed_result, dict):
            skipped_count += 1
            continue
        if merge_parsed_result_into_storage(storage, parsed_result, fallback_post_url=fallback_post_url):
            merged_count += 1
        else:
            skipped_count += 1

    update_meta(storage)
    atomic_write_json(storage_path, storage)
    return {
        "merged_posts": merged_count,
        "skipped_posts": skipped_count,
        "storage_path": str(storage_path),
        "total_profiles": storage.get("meta", {}).get("total_profiles"),
        "total_posts": storage.get("meta", {}).get("total_posts"),
    }


def _is_within_directory(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def cleanup_dump_dirs(output_root: Path, dump_dirs: list[str]) -> dict[str, Any]:
    deleted_dump_dirs: list[str] = []
    cleanup_errors: list[str] = []

    output_root_abs = output_root.resolve()

    for dump_dir in dump_dirs:
        if not dump_dir:
            continue

        try:
            candidate = Path(dump_dir).resolve()
        except Exception as exc:
            cleanup_errors.append(f"Failed to resolve cleanup path '{dump_dir}': {exc!r}")
            continue

        # Safety check before recursive delete.
        if not _is_within_directory(candidate, output_root_abs):
            cleanup_errors.append(f"Skipped unsafe cleanup path outside '{output_root_abs}': {candidate}")
            continue

        if not candidate.exists():
            continue

        try:
            shutil.rmtree(candidate)
            deleted_dump_dirs.append(str(candidate))
        except Exception as exc:
            cleanup_errors.append(f"Failed to remove '{candidate}': {exc!r}")

    try:
        if output_root_abs.exists() and not any(output_root_abs.iterdir()):
            output_root_abs.rmdir()
    except Exception:
        pass

    return {
        "deleted_dump_dirs": deleted_dump_dirs,
        "cleanup_errors": cleanup_errors,
    }


async def run_capture_and_parse(
    post_urls: list[str],
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    normalized_urls = normalize_post_urls(post_urls)
    if not normalized_urls:
        raise ValueError("Provide at least one valid Instagram post or reel URL in 'post_urls'.")

    _emit_progress(on_progress, f"Capture pipeline started for {len(normalized_urls)} normalized URLs.")
    run_started_at = utc_now_iso()
    output_root = DEFAULT_OUTPUT_ROOT
    output_root.mkdir(parents=True, exist_ok=True)

    captured_results: list[dict[str, Any]] = []
    parsed_payloads: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    dump_dirs: list[str] = []
    merge_summary: dict[str, Any] | None = None
    run_error: str | None = None

    try:
        captured_results = await capture_posts_parallel(
            post_urls=normalized_urls,
            output_root=output_root,
            concurrency=DEFAULT_CONCURRENCY,
            scroll_times=DEFAULT_SCROLL_TIMES,
            scroll_step=DEFAULT_SCROLL_STEP,
            min_scroll_gap_ms=DEFAULT_MIN_SCROLL_GAP_MS,
            max_scroll_gap_ms=DEFAULT_MAX_SCROLL_GAP_MS,
            timeout_ms=DEFAULT_TIMEOUT_MS,
            on_progress=on_progress,
        )
        dump_dirs = [str(item.get("dump_dir")) for item in captured_results if item.get("dump_dir")]

        for capture in captured_results:
            if not capture.get("ok"):
                failures.append(capture)
                continue
            html_path = capture.get("html_path")
            if not html_path:
                failures.append(capture)
                continue
            try:
                parsed_result = parse_html_file(html_path)
                _emit_progress(on_progress, f"Parsed HTML successfully for {capture.get('post_url')}.")
                parsed_payloads.append(
                    {
                        "post_url": capture.get("post_url"),
                        "html_path": html_path,
                        "parsed_result": parsed_result,
                    }
                )
            except Exception as exc:
                failures.append(
                    {
                        "post_url": capture.get("post_url"),
                        "ok": False,
                        "html_path": html_path,
                        "error": f"parse_error: {exc}",
                    }
                )
                _emit_progress(on_progress, f"Parse failed for {capture.get('post_url')}: {exc}")

        merge_summary = merge_parsed_results(
            storage_path=DEFAULT_PROFILES_JSON,
            parsed_payloads=parsed_payloads,
        )
        _emit_progress(
            on_progress,
            (
                f"Merge completed: merged_posts={merge_summary.get('merged_posts')} "
                f"skipped_posts={merge_summary.get('skipped_posts')}"
            ),
        )
    except Exception as exc:
        run_error = repr(exc)
        _emit_progress(on_progress, f"Capture pipeline failed: {run_error}")
    finally:
        cleanup_summary = cleanup_dump_dirs(output_root=output_root, dump_dirs=dump_dirs)
        _emit_progress(
            on_progress,
            (
                f"Cleanup completed: deleted_dump_dirs={len(cleanup_summary.get('deleted_dump_dirs') or [])} "
                f"cleanup_errors={len(cleanup_summary.get('cleanup_errors') or [])}"
            ),
        )

    return {
        "ok": run_error is None,
        "error": run_error,
        "started_at": run_started_at,
        "finished_at": utc_now_iso(),
        "captured_total": len(captured_results),
        "parsed_total": len(parsed_payloads),
        "failures": failures,
        "merge": merge_summary,
        "captures": captured_results,
        "cleanup": cleanup_summary,
    }


def run_capture_and_parse_blocking(
    post_urls: list[str],
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    # Playwright subprocess startup on Windows needs a Proactor loop.
    if sys.platform == "win32":  # pragma: no cover
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    return asyncio.run(run_capture_and_parse(post_urls, on_progress=on_progress))
