# -*- coding: utf-8 -*-
"""RSS feed fetcher — uses feedparser."""

from typing import Dict, Any, List

import feedparser
import requests
from loguru import logger

from sf_reader_all.utils.async_runtime import run_blocking

# Note: feedparser already handles XXE safely by default (uses xml.sax with entity expansion limits)
# Additional protection: resolve_relative_uris=False disables relative URL resolution

RSS_TIMEOUT = 20


def _fetch_rss_sync(url: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Fetch and parse an RSS/Atom feed.

    Args:
        url: RSS feed URL
        limit: Max number of entries to return

    Returns:
        List of article dicts with: title, summary, url, source, published
    """
    logger.info(f"Fetching RSS: {url}")

    response = requests.get(url, timeout=RSS_TIMEOUT)
    response.raise_for_status()

    # Parse downloaded bytes so feedparser never performs its own unbounded
    # network request. Relative URL resolution remains disabled.
    feed = feedparser.parse(response.content, resolve_relative_uris=False)

    if feed.bozo and not feed.entries:
        raise ValueError(f"Failed to parse RSS feed: {feed.bozo_exception}")

    source_name = feed.feed.get("title", url)
    articles = []

    for entry in feed.entries[:limit]:
        summary = ""
        if hasattr(entry, "summary"):
            summary = entry.summary
        elif hasattr(entry, "content"):
            summary = entry.content[0].get("value", "")

        articles.append({
            "title": entry.get("title", ""),
            "summary": summary,
            "url": entry.get("link", ""),
            "source": source_name,
            "published": entry.get("published", ""),
            "platform": "rss",
        })

    logger.info(f"RSS: {len(articles)} articles from {source_name}")
    return articles


async def fetch_rss(url: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch and parse an RSS/Atom feed without blocking the event loop."""
    return await run_blocking(_fetch_rss_sync, url, limit)
