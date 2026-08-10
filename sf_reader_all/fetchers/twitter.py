# -*- coding: utf-8 -*-
"""
X/Twitter fetcher — four-tier fallback:

1. FxTwitter API (full text, structured JSON, no auth needed)
2. X oEmbed API (fast, but truncates long tweets)
3. Jina Reader (handles non-tweet X pages like profiles)
4. Playwright + saved session (handles login-required content)

Install browser tier: pip install "sf-reader-all[browser]" && playwright install chromium
Save X session:       sf-reader-all login twitter
"""

import asyncio
import re
import requests
from loguru import logger
from pathlib import Path
from typing import Dict, Any

from sf_reader_all.fetchers.browser_runtime import (
    BrowserMode,
    BrowserRuntime,
    use_browser_runtime,
)
from sf_reader_all.fetchers.jina import fetch_via_jina_async
from sf_reader_all.utils.async_runtime import run_blocking


FXTWITTER_API = "https://api.fxtwitter.com"
OEMBED_URL = "https://publish.twitter.com/oembed"


def _extract_author(url: str) -> str:
    """Extract @username from tweet URL."""
    match = re.search(r'x\.com/(\w+)/status', url)
    return f"@{match.group(1)}" if match else ""


def _is_tweet_url(url: str) -> bool:
    """Check if this is a direct tweet/status URL (vs profile or other X page)."""
    return bool(re.search(r'x\.com/\w+/status/\d+', url))


def _fetch_via_fxtwitter(url: str) -> Dict[str, Any]:
    """
    Fetch full tweet text via FxTwitter API.
    Free, no auth, returns complete text (no truncation).
    """
    match = re.search(r'x\.com/(\w+)/status/(\d+)', url)
    if not match:
        raise ValueError(f"Cannot parse tweet URL: {url}")

    username, status_id = match.group(1), match.group(2)
    api_url = f"{FXTWITTER_API}/{username}/status/{status_id}"

    resp = requests.get(api_url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    tweet = data.get("tweet", {})
    text = tweet.get("text", "")
    author_name = tweet.get("author", {}).get("name", "")
    author_screen = tweet.get("author", {}).get("screen_name", "")

    return {
        "text": text,
        "author": f"@{author_screen}" if author_screen else "",
        "author_name": author_name,
        "title": text[:100] if text else "",
    }


def _fetch_via_oembed(url: str) -> Dict[str, Any]:
    """
    Fetch tweet text via X's oEmbed API.
    Free, reliable, no auth needed. Works for public tweets.
    Note: oEmbed requires twitter.com URLs (not x.com).
    """
    # oEmbed API requires twitter.com format
    oembed_query_url = url.replace("x.com", "twitter.com")
    resp = requests.get(
        OEMBED_URL,
        params={"url": oembed_query_url, "omit_script": "true"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    # Strip HTML tags from the embedded HTML to get clean text
    html = data.get("html", "")
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()

    return {
        "text": text,
        "author": data.get("author_name", ""),
        "author_url": data.get("author_url", ""),
        "title": text[:100] if text else "",
    }


async def _fetch_via_playwright(
    url: str,
    *,
    runtime: BrowserRuntime | None = None,
    timeout_ms: int = 30_000,
) -> Dict[str, Any]:
    """
    Fetch tweet via Playwright with X-specific DOM selectors.
    Uses saved login session if available (~/.sf-reader-all/sessions/twitter.json).
    """
    from sf_reader_all.fetchers.browser import get_session_path

    session_path = get_session_path("twitter")
    has_session = Path(session_path).exists()
    if has_session:
        logger.info(f"Using saved X session: {session_path}")

    try:
        return await asyncio.wait_for(
            _fetch_playwright_with_runtime(
                url,
                storage_state=session_path if has_session else None,
                runtime=runtime,
                operation_timeout_ms=timeout_ms,
            ),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Twitter browser fetch exceeded {timeout_ms} ms total deadline: {url}"
        ) from exc


async def _fetch_playwright_with_runtime(
    url: str,
    *,
    storage_state: str | None,
    runtime: BrowserRuntime | None,
    operation_timeout_ms: int,
) -> Dict[str, Any]:
    """Include runtime/browser/page acquisition in the fallback deadline."""
    async with use_browser_runtime(runtime) as active_runtime:
        async with active_runtime.page(
            mode=BrowserMode.STEALTH,
            storage_state=storage_state,
        ) as page:
            return await _load_twitter_page(page, url, operation_timeout_ms)


async def _load_twitter_page(page, url: str, timeout_ms: int) -> Dict[str, Any]:
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    # X is a SPA. Continue as soon as any useful fallback node has text; a
    # login wall may never render tweetText, so the condition includes article
    # and main instead of sleeping for the full selector timeout.
    try:
        await page.wait_for_function(
            """() => {
                const el = document.querySelector('[data-testid="tweetText"]')
                    || document.querySelector('article')
                    || document.querySelector('main');
                return Boolean(el && el.innerText && el.innerText.trim());
            }""",
            timeout=min(10_000, timeout_ms),
        )
    except Exception:
        logger.warning("[Twitter] browser DOM did not become ready; extracting current page")

    # Extract tweet content with X-specific selectors
    tweet_text = await page.evaluate("""() => {
                // Priority 1: tweet text element
                const tweetEl = document.querySelector('[data-testid="tweetText"]');
                if (tweetEl) return tweetEl.innerText;

                // Priority 2: article element (thread view)
                const article = document.querySelector('article');
                if (article) return article.innerText;

                // Priority 3: main content area
                const main = document.querySelector('main');
                if (main) return main.innerText;

                return '';
            }""")

    title = await page.title()

    return {
        "text": (tweet_text or "").strip(),
        "title": (title or "").strip()[:200],
    }


async def fetch_twitter(
    url: str,
    *,
    runtime: BrowserRuntime | None = None,
    browser_timeout_ms: int = 30_000,
) -> Dict[str, Any]:
    """
    Fetch a tweet or X post with four-tier fallback.

    Args:
        url: Tweet URL (x.com or twitter.com)
        runtime: Entered BrowserRuntime to reuse for the Playwright fallback.
        browser_timeout_ms: Total deadline for the Playwright fallback tier.

    Returns:
        Dict with: text, author, url, title, platform
    """
    url = url.replace("twitter.com", "x.com")
    author = _extract_author(url)

    # Tier 1: FxTwitter API (full text, no truncation)
    if _is_tweet_url(url):
        try:
            logger.info(f"[Twitter] Tier 1 — FxTwitter: {url}")
            data = await run_blocking(_fetch_via_fxtwitter, url)
            text = (data.get("text") or "").strip()
            if text:
                return {
                    "text": text,
                    "author": author or data.get("author", ""),
                    "url": url,
                    "title": data.get("title", ""),
                    "platform": "twitter",
                }
            logger.warning("[Twitter] FxTwitter returned empty text")
        except Exception as e:
            logger.warning(f"[Twitter] FxTwitter failed ({e})")

    # Tier 2: oEmbed API (fast but truncates long tweets)
    if _is_tweet_url(url):
        try:
            logger.info(f"[Twitter] Tier 2 — oEmbed: {url}")
            data = await run_blocking(_fetch_via_oembed, url)
            text = (data.get("text") or "").strip()
            thin_oembed = (
                len(text) <= 20
                or text.lower().startswith("https://t.co/")
                or ("&mdash;" in text and text.count("https://t.co/") >= 1)
            )
            if not thin_oembed:
                return {
                    "text": text,
                    "author": author or data.get("author", ""),
                    "url": url,
                    "title": data.get("title", ""),
                    "platform": "twitter",
                }
            logger.warning("[Twitter] oEmbed returned thin content")
        except Exception as e:
            logger.warning(f"[Twitter] oEmbed failed ({e})")

    # Tier 3: Jina Reader (handles profiles, threads, non-tweet pages)
    try:
        logger.info(f"[Twitter] Tier 3 — Jina: {url}")
        data = await fetch_via_jina_async(url)
        content = data.get("content", "")
        title = data.get("title", "")
        jina_ok = (
            content
            and len(content.strip()) > 100
            and "not yet fully loaded" not in content.lower()
            and title.lower() not in ("x", "title: x", "")
        )
        if jina_ok:
            return {
                "text": content,
                "author": author,
                "url": url,
                "title": title,
                "platform": "twitter",
            }
        logger.warning("[Twitter] Jina returned unusable content")
    except Exception as e:
        logger.warning(f"[Twitter] Jina failed ({e})")

    # Tier 4: Playwright + session with X-specific extraction
    try:
        logger.info(f"[Twitter] Tier 4 — Playwright: {url}")
        data = await _fetch_via_playwright(
            url,
            runtime=runtime,
            timeout_ms=browser_timeout_ms,
        )
        content = data.get("text", "")
        if content and len(content.strip()) > 20:
            return {
                "text": content,
                "author": author,
                "url": url,
                "title": data.get("title", ""),
                "platform": "twitter",
            }
        logger.warning("[Twitter] Playwright returned empty content")
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"[Twitter] All methods failed: {e}")

    raise RuntimeError(
        f"❌ All Twitter fetch methods failed for: {url}\n"
        f"   Try: sf-reader-all login twitter (to save session for browser fallback)\n"
        f"   Then retry: sf-reader-all {url}"
    )
