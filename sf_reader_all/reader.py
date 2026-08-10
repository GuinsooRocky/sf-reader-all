# -*- coding: utf-8 -*-
"""
Universal Reader — routes any URL to the right fetcher.

The core dispatcher: give it a URL, get back structured content.
"""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from loguru import logger
from typing import Dict, Any, Optional

from sf_reader_all.schema import (
    UnifiedContent, UnifiedInbox, SourceType,
    from_bilibili, from_twitter, from_wechat,
    from_xiaohongshu, from_youtube, from_rss, from_telegram,
)
from sf_reader_all.fetchers.jina import fetch_via_jina_async
from sf_reader_all.fetchers.browser_runtime import BrowserRuntime
from sf_reader_all.utils.async_runtime import run_blocking
from sf_reader_all.utils.url_validator import validate_url


ACTIVE_READ_CONCURRENCY = 16


class UniversalReader:
    """
    Routes URLs to platform-specific fetchers and local files to parsers.
    Falls back to Jina Reader for unknown platforms.
    """

    def __init__(self, inbox: Optional[UnifiedInbox] = None):
        self.inbox = inbox
        self._persist_lock = asyncio.Lock()

    def _detect_platform(self, url: str) -> str:
        """Detect platform from URL."""
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")

        def is_domain(domain: str) -> bool:
            return hostname == domain or hostname.endswith(f".{domain}")

        if is_domain("mp.weixin.qq.com"):
            return "wechat"
        if is_domain("x.com") or is_domain("twitter.com"):
            return "twitter"
        if is_domain("youtube.com") or is_domain("youtu.be"):
            return "youtube"
        if is_domain("xiaohongshu.com") or is_domain("xhslink.com"):
            return "xhs"
        if is_domain("bilibili.com") or is_domain("b23.tv"):
            return "bilibili"
        if is_domain("xiaoyuzhoufm.com"):
            return "podcast"
        if is_domain("podcasts.apple.com"):
            return "podcast"
        if is_domain("t.me") or is_domain("telegram.org"):
            return "telegram"
        if url.endswith(".xml") or "/rss" in url or "/feed" in url or "/atom" in url:
            return "rss"
        return "generic"

    def _persist_many(self, contents: list[UnifiedContent]) -> None:
        """Persist a completed read operation through one storage boundary."""
        if not contents:
            return

        if self.inbox:
            changed = self.inbox.add_batch(contents)
            if changed or self.inbox.is_dirty:
                self.inbox.save()
                logger.info(f"Saved {changed} item(s) to inbox")

        from sf_reader_all.utils.storage import save_many_to_markdown
        save_many_to_markdown(contents)

    def _persist(self, content: UnifiedContent) -> None:
        """Preserve the single-item persistence behavior."""
        self._persist_many([content])

    async def _persist_many_async(self, contents: list[UnifiedContent]) -> None:
        """Persist off-loop while serializing access to this reader's inbox."""
        if not contents:
            return
        async with self._persist_lock:
            worker = asyncio.create_task(run_blocking(self._persist_many, contents))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # The thread cannot be stopped safely. Keep the lock until its
                # write finishes, then preserve cancellation for the caller.
                try:
                    await worker
                except Exception as error:
                    logger.error(f"Persistence failed during cancellation: {error}")
                raise

    async def _read_file(
        self, path: str | os.PathLike[str], *, persist: bool
    ) -> UnifiedContent:
        from sf_reader_all.parsers.document import read_document

        try:
            content = await run_blocking(read_document, path)
            if persist:
                await self._persist_many_async([content])
            return content
        except Exception as e:
            logger.error(f"[document] Failed: {e}")
            raise

    async def read_file(self, path: str | os.PathLike[str]) -> UnifiedContent:
        """Convert a local document and return it as UnifiedContent."""
        return await self._read_file(path, persist=True)

    async def _read_source(
        self,
        source: str | os.PathLike[str],
        *,
        persist: bool,
        browser_runtime: BrowserRuntime | None = None,
    ) -> UnifiedContent:
        source_text = os.fspath(source)
        is_remote = source_text.startswith(("http://", "https://"))
        if isinstance(source, os.PathLike) or not is_remote:
            candidate = Path(source_text).expanduser()
            if candidate.is_file() or isinstance(source, os.PathLike):
                return await self._read_file(candidate, persist=persist)
        return await self._read_url(
            source_text,
            persist=persist,
            browser_runtime=browser_runtime,
        )

    async def read_source(self, source: str | os.PathLike[str]) -> UnifiedContent:
        """Read either an existing local document or a URL."""
        return await self._read_source(source, persist=True)

    async def _read_url(
        self,
        url: str,
        *,
        persist: bool,
        browser_runtime: BrowserRuntime | None = None,
    ) -> UnifiedContent:
        # Ensure URL has scheme
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        # SSRF protection: block private IPs, metadata endpoints, DNS rebinding
        await run_blocking(validate_url, url)

        platform = self._detect_platform(url)
        logger.info(f"[{platform}] {url[:60]}...")

        try:
            content = await self._fetch(
                platform, url, browser_runtime=browser_runtime)

            if persist:
                await self._persist_many_async([content])
            return content

        except Exception as e:
            logger.error(f"[{platform}] Failed: {e}")
            raise

    async def read(self, url: str) -> UnifiedContent:
        """
        Fetch content from a URL and return as UnifiedContent.

        The URL-only entry point used by the MCP server.
        """
        return await self._read_url(url, persist=True)

    async def _fetch(
        self,
        platform: str,
        url: str,
        *,
        browser_runtime: BrowserRuntime | None = None,
    ) -> UnifiedContent:
        """Dispatch to platform-specific fetcher."""

        if platform == "bilibili":
            from sf_reader_all.fetchers.bilibili import fetch_bilibili
            data = await fetch_bilibili(url)
            return from_bilibili(data)

        if platform == "twitter":
            from sf_reader_all.fetchers.twitter import fetch_twitter
            data = await fetch_twitter(url, runtime=browser_runtime)
            return from_twitter(data)

        if platform == "wechat":
            from sf_reader_all.fetchers.wechat import fetch_wechat
            data = await fetch_wechat(url, runtime=browser_runtime)
            return from_wechat(data)

        if platform == "xhs":
            from sf_reader_all.fetchers.xhs import fetch_xhs
            data = await fetch_xhs(url, runtime=browser_runtime)
            return from_xiaohongshu(data)

        if platform == "youtube":
            from sf_reader_all.fetchers.youtube import fetch_youtube
            data = await fetch_youtube(url)
            return from_youtube(data)

        if platform == "rss":
            from sf_reader_all.fetchers.rss import fetch_rss
            articles = await fetch_rss(url, limit=1)
            if articles:
                return from_rss(articles[0])
            raise ValueError(f"No articles found in RSS feed: {url}")

        if platform == "telegram":
            from sf_reader_all.fetchers.telegram import fetch_telegram
            # Extract channel username from t.me URL
            path = urlparse(url).path.strip("/").split("/")[0]
            channel = path if path else url
            messages = await fetch_telegram(channel, limit=1)
            if messages:
                return from_telegram(messages[0], channel, channel)
            raise ValueError(f"No messages from Telegram channel: {url}")

        # Fallback: Jina Reader for any unknown URL
        logger.info(f"Using Jina fallback for: {url}")
        data = await fetch_via_jina_async(url)
        return UnifiedContent(
            source_type=SourceType.MANUAL,
            source_name=urlparse(url).netloc,
            title=data["title"],
            content=data["content"],
            url=url,
        )

    async def _read_many(self, sources, read_one) -> list[UnifiedContent]:
        """Run reads concurrently, then persist their successes as one batch."""
        read_slots = asyncio.Semaphore(ACTIVE_READ_CONCURRENCY)

        async def limited_read(source, browser_runtime):
            async with read_slots:
                return await read_one(
                    source,
                    persist=False,
                    browser_runtime=browser_runtime,
                )

        async with BrowserRuntime() as browser_runtime:
            tasks = [
                limited_read(source, browser_runtime)
                for source in sources
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        contents = []
        for source, result in zip(sources, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                logger.error(f"Batch failed for {source}: {result}")
            elif isinstance(result, BaseException):
                raise result
            else:
                contents.append(result)

        await self._persist_many_async(contents)
        return contents

    async def read_batch(self, urls: list[str]) -> list[UnifiedContent]:
        """Fetch multiple URLs concurrently."""
        return await self._read_many(urls, self._read_url)

    async def read_sources(self, sources: list[str]) -> list[UnifiedContent]:
        """Read multiple URLs or local documents concurrently."""
        return await self._read_many(sources, self._read_source)
