# -*- coding: utf-8 -*-
"""
Jina Reader — universal fallback for content extraction.

Uses https://r.jina.ai/{url} to extract markdown from any web page.
Handles JS rendering and anti-scraping.

NOTE (2026-06): anonymous access now returns 401 — r.jina.ai requires an
API key. Set JINA_API_KEY in the environment (free tier available at
https://jina.ai/reader); without it this fetcher will fail and callers
fall back to other channels.
"""

import os

import requests
from loguru import logger


JINA_BASE = "https://r.jina.ai"
TIMEOUT = 30

HEADERS = {
    "Accept": "text/markdown",
    "User-Agent": "sf-reader-all/0.1",
}


def _headers() -> dict:
    key = os.getenv("JINA_API_KEY")
    return {**HEADERS, "Authorization": f"Bearer {key}"} if key else dict(HEADERS)


def fetch_via_jina(url: str) -> dict:
    """
    Fetch any URL via Jina Reader and return structured data.

    Returns:
        dict with keys: title, content, url, author (best-effort)
    """
    jina_url = f"{JINA_BASE}/{url}"
    logger.info(f"Jina fetch: {url}")

    try:
        resp = requests.get(jina_url, headers=_headers(), timeout=TIMEOUT)
        resp.raise_for_status()
        text = resp.text

        # Jina returns markdown; first line is usually the title
        lines = text.strip().split("\n")
        title = ""
        content_lines = []

        for line in lines:
            if not title and line.strip():
                # First non-empty line as title, strip markdown heading
                title = line.lstrip("#").strip()
            else:
                content_lines.append(line)

        content = "\n".join(content_lines).strip()

        return {
            "title": title[:200],
            "content": content,
            "url": url,
            "author": "",
        }

    except requests.Timeout:
        logger.error(f"Jina timeout: {url}")
        raise
    except requests.RequestException as e:
        logger.error(f"Jina fetch failed: {url} — {e}")
        raise
