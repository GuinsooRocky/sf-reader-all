# -*- coding: utf-8 -*-
"""
Playwright browser fetcher — headless Chromium fallback for anti-scraping sites.

Used when Jina Reader fails (403/451/timeout). Supports persistent login
sessions via Playwright's storage_state for platforms requiring authentication.

Install: pip install "sf-reader-all[browser]" && playwright install chromium
"""

import asyncio
from pathlib import Path

from loguru import logger

from sf_reader_all.fetchers.browser_runtime import (
    BrowserMode,
    BrowserRuntime,
    use_browser_runtime,
)
from sf_reader_all.utils.async_runtime import run_blocking

SESSION_DIR = Path.home() / ".sf-reader-all" / "sessions"
TIMEOUT_MS = 30_000


async def fetch_via_browser(
    url: str,
    storage_state: str = None,
    stealth: bool = False,
    *,
    runtime: BrowserRuntime | None = None,
    timeout_ms: int = TIMEOUT_MS,
) -> dict:
    """
    Fetch a URL using headless Chromium via Playwright.

    Args:
        url: Target URL to fetch.
        storage_state: Path to a Playwright storage state JSON file (cookies/localStorage).
                       If provided, the browser context will load this session.
        stealth: If True, launch real Chrome + anti-automation flag + direct
                 connection (no proxy). Required for hardened anti-scrape sites
                 like WeChat — bundled Chromium or a proxied request trips CAPTCHA.
        runtime: Entered BrowserRuntime to reuse across fetches. When omitted,
                 this call owns and closes a temporary runtime.
        timeout_ms: Total navigation, readiness, and extraction deadline.

    Returns:
        dict with keys: title, content, url, author
    """
    # Security: Validate URL before fetching
    from sf_reader_all.utils.url_validator import validate_url
    await run_blocking(validate_url, url)

    logger.info(f"Browser fetch: {url}")

    mode = BrowserMode.STEALTH_DIRECT if stealth else BrowserMode.STANDARD
    try:
        result = await asyncio.wait_for(
            _fetch_with_runtime(
                url,
                mode=mode,
                storage_state=storage_state,
                runtime=runtime,
                operation_timeout_ms=timeout_ms,
            ),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Browser fetch exceeded {timeout_ms} ms total deadline: {url}"
        ) from exc

    logger.info(f"Browser fetch OK: {result['title'][:60]}")
    return result


async def _fetch_with_runtime(
    url: str,
    *,
    mode: BrowserMode,
    storage_state: str | None,
    runtime: BrowserRuntime | None,
    operation_timeout_ms: int,
) -> dict:
    """Include runtime/browser/page acquisition in the caller's deadline."""
    async with use_browser_runtime(runtime) as active_runtime:
        async with active_runtime.page(
            mode=mode,
            storage_state=storage_state,
        ) as page:
            return await _load_and_extract(page, url, operation_timeout_ms)


async def _load_and_extract(page, url: str, timeout_ms: int) -> dict:
    """Navigate, wait for platform content, and extract within caller deadline."""
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    is_xhs = "xiaohongshu.com" in url or "xhslink.com" in url
    is_wechat = "mp.weixin.qq.com" in url

    if is_xhs:
        # The container alone can precede the SPA content. Wait until either
        # title or body text is populated, then extract immediately.
        try:
            await page.wait_for_function(
                """() => {
                    const root = document.querySelector('#noteContainer');
                    if (!root) return false;
                    const title = document.querySelector('#detail-title');
                    const desc = document.querySelector('#detail-desc');
                    return Boolean(
                        (title && title.innerText.trim())
                        || (desc && desc.innerText.trim())
                        || root.innerText.trim()
                    );
                }""",
                timeout=min(8_000, timeout_ms),
            )
        except Exception:
            logger.warning("[XHS] note content not ready within 8s, proceeding anyway")

        data = await page.evaluate("""() => {
                    const title = document.querySelector('#detail-title');
                    const desc = document.querySelector('#detail-desc');
                    const meta = document.querySelector('.bottom-container');
                    const author = document.querySelector('.author-wrapper .username')
                        || document.querySelector('.interaction-container');
                    return {
                        title: title ? title.innerText.trim() : '',
                        content: [
                            desc ? desc.innerText.trim() : '',
                            meta ? meta.innerText.trim() : '',
                        ].filter(Boolean).join('\\n\\n'),
                        author: author ? author.innerText.trim().split('\\n')[0] : '',
                    };
                }""")

        return {
            "title": (data["title"] or "").strip()[:200],
            "content": (data["content"] or "").strip(),
            "url": page.url,
            "author": (data["author"] or "").strip(),
        }

    if is_wechat:
        # Readiness is tied to an article container, rather than a fixed sleep.
        try:
            await page.wait_for_selector(
                "#js_content, .rich_media_content, article, main",
                timeout=min(8_000, timeout_ms),
            )
        except Exception:
            logger.warning("[WeChat] article container not ready, using generic body")

        title = await page.title()
        metadata = await page.evaluate("""() => {
                    const title = document.querySelector('#activity-name')
                        || document.querySelector('.rich_media_title');
                    const author = document.querySelector('#js_name')
                        || document.querySelector('.rich_media_meta_nickname');
                    const ogTitle = document.querySelector('meta[property="og:title"]');
                    return {
                        title: (title && title.innerText.trim())
                            || (ogTitle && ogTitle.content.trim())
                            || '',
                        author: author ? author.innerText.trim() : '',
                    };
                }""")
        content = await page.evaluate("""() => {
                    const container = document.querySelector('#js_content') || document.querySelector('.rich_media_content');
                    if (!container) return null; // Safe fallback to generic if not found
                    
                    let elements = [];
                    const walk = (node) => {
                        if (node.tagName === 'IMG') {
                           let src = node.getAttribute('data-src') || node.getAttribute('src');
                           if (src) elements.push(`![image](${src})`);
                        } else if (node.nodeType === 3) { // Text node
                           let text = node.textContent.trim();
                           if (text) elements.push(text);
                        } else if (node.nodeType === 1) { // Element node
                           for (let child of node.childNodes) walk(child);
                        }
                    };
                    walk(container);
                    return elements.join('\\n\\n');
                }""")

        # Generic fallback if WeChat specific extraction yields nothing
        if not content:
            content = await page.evaluate("""() => {
                        const el = document.querySelector('article')
                            || document.querySelector('main')
                            || document.querySelector('.content')
                            || document.body;
                        return el ? el.innerText : '';
                    }""")

        return {
            "title": (metadata["title"] or title or "").strip()[:200],
            "content": (content or "").strip(),
            "url": page.url,
            "author": (metadata["author"] or "").strip(),
        }

    # Generic pages become ready when the preferred content node has text.
    try:
        await page.wait_for_function(
            """() => {
                const el = document.querySelector('article')
                    || document.querySelector('main')
                    || document.querySelector('.content')
                    || document.body;
                return Boolean(el && el.innerText && el.innerText.trim());
            }""",
            timeout=min(8_000, timeout_ms),
        )
    except Exception:
        logger.warning("[browser] page content not ready, extracting current DOM")

    title = await page.title()
    content = await page.evaluate("""() => {
                    const el = document.querySelector('article')
                        || document.querySelector('main')
                        || document.querySelector('.content')
                        || document.body;
                    return el ? el.innerText : '';
                }""")

    return {
        "title": (title or "").strip()[:200],
        "content": (content or "").strip(),
        "url": page.url,
        "author": "",
    }


def get_session_path(platform: str) -> str:
    """Get the session file path for a platform."""
    return str(SESSION_DIR / f"{platform}.json")
