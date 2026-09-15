# -*- coding: utf-8 -*-
"""
WeChat article fetcher — Playwright stealth only.

Jina Reader used to be tier 1, but WeChat serves it the CAPTCHA stub every
time, so the round trip (~5s) was pure waste. Public articles need no login,
but bundled headless Chromium or a proxied request trips WeChat's CAPTCHA —
stealth=True is required (real Chrome + anti-automation + direct/no-proxy).
"""

import re
from loguru import logger
from typing import Dict, Any

from sf_reader_all.fetchers.browser_runtime import BrowserRuntime


def _proxy_wechat_images(content: str) -> str:
    """Replace WeChat image URLs with a proxy to bypass anti-hotlinking."""
    if not content:
        return content
    return re.sub(
        r'(https?://mmbiz\.qpic\.cn/[^\s\)]+)',
        r'https://wsrv.nl/?url=\1',
        content
    )


async def fetch_wechat(
    url: str, *, runtime: BrowserRuntime | None = None
) -> Dict[str, Any]:
    """
    Fetch a WeChat public account article via stealth browser.

    Args:
        url: mp.weixin.qq.com article URL

    Returns:
        Dict with: title, content, author, url, platform
    """
    try:
        logger.info(f"[WeChat] Playwright stealth (real Chrome, direct): {url}")
        from sf_reader_all.fetchers.browser import fetch_via_browser

        data = await fetch_via_browser(url, stealth=True, runtime=runtime)
        return {
            "title": data["title"],
            "content": _proxy_wechat_images(data["content"]),
            "author": data.get("author", ""),
            "url": url,
            "platform": "wechat",
        }
    except RuntimeError:
        # Playwright not installed — message already tells how to fix it
        raise
    except Exception as e:
        logger.error(f"[WeChat] Browser fetch failed: {e}")
        raise RuntimeError(
            f"❌ WeChat fetch failed.\n"
            f"   Last error: {e}"
        )
