# -*- coding: utf-8 -*-
"""Reusable Playwright browser runtime.

``BrowserRuntime`` is the lifecycle seam for async browser fetchers.  A caller
can share one runtime across many fetches; browsers and contexts with the same
configuration are reused, while every fetch still receives its own page.

The runtime must be entered before it is passed to a fetcher::

    async with BrowserRuntime() as runtime:
        first = await fetch_via_browser(url_a, runtime=runtime)
        second = await fetch_via_browser(url_b, runtime=runtime)

Fetchers create an owned runtime when this argument is omitted, preserving the
standalone behaviour and the optional Playwright dependency.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

from loguru import logger


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

PLAYWRIGHT_INSTALL_ERROR = (
    "Playwright is not installed. Run:\n"
    '  pip install "sf-reader-all[browser]"\n'
    "  playwright install chromium"
)

DEFAULT_PAGE_CONCURRENCY = 6


class BrowserMode(str, Enum):
    """Supported launch behaviours hidden behind the runtime seam."""

    STANDARD = "standard"
    STEALTH = "stealth"
    STEALTH_DIRECT = "stealth-direct"


@dataclass(frozen=True)
class _BrowserKey:
    mode: BrowserMode
    headless: bool


@dataclass(frozen=True)
class _ContextKey:
    browser: _BrowserKey
    storage_state: str | None
    viewport: tuple[int, int] | None


async def _start_playwright() -> Any:
    """Import and start Playwright lazily so the browser extra stays optional."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(PLAYWRIGHT_INSTALL_ERROR) from exc
    return await async_playwright().start()


class BrowserRuntime:
    """Own and reuse Playwright browsers and contexts.

    Interface invariants:
    - enter the runtime with ``async with`` before calling :meth:`page`;
    - at most six pages are active by default across all cached contexts;
    - pages are private to one ``page`` block and are always closed on exit;
    - contexts are keyed by mode, headless setting, session, and viewport;
    - the caller that enters the runtime owns its final close.
    """

    def __init__(self, *, max_pages: int = DEFAULT_PAGE_CONCURRENCY) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self._playwright: Any | None = None
        self._browsers: dict[_BrowserKey, Any] = {}
        self._contexts: dict[_ContextKey, Any] = {}
        self._create_lock = asyncio.Lock()
        self._page_slots = asyncio.Semaphore(max_pages)
        self._entered = False
        self._closing = False
        self._closed = False

    async def __aenter__(self) -> "BrowserRuntime":
        if self._closed or self._closing:
            raise RuntimeError("BrowserRuntime cannot be reused after it is closed")
        # Entering is intentionally cheap: a batch containing only HTTP-backed
        # fetchers can carry a runtime without importing or starting Playwright.
        # The first page request starts it under the creation lock.
        self._entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    @asynccontextmanager
    async def page(
        self,
        *,
        mode: BrowserMode = BrowserMode.STANDARD,
        headless: bool = True,
        storage_state: str | Path | None = None,
        viewport: tuple[int, int] | None = None,
    ) -> AsyncIterator[Any]:
        """Yield a fresh page backed by a cached browser context.

        ``storage_state`` is loaded once, when its cached context is created.
        The context (including cookies changed during use) then lives until the
        runtime exits. Missing session files are ignored for compatibility with
        the original standalone browser fetcher.
        """
        self._ensure_open()
        slot_acquired = False
        page = None
        try:
            # This semaphore is global to the runtime, so different browser
            # modes and contexts still share one bounded page budget.
            await self._page_slots.acquire()
            slot_acquired = True

            mode = BrowserMode(mode)
            session = self._prepare_storage_state(storage_state)
            browser_key = _BrowserKey(mode=mode, headless=headless)
            context_key = _ContextKey(
                browser=browser_key,
                storage_state=session,
                viewport=viewport,
            )

            # Creation and close share the same lock. close() marks its intent
            # before waiting for this lock; the checks after each awaited
            # creation prevent a resource completed during that race from ever
            # being yielded after shutdown has begun.
            async with self._create_lock:
                self._ensure_open()
                context = await self._get_context_locked(context_key)
                self._ensure_open()
                page = await context.new_page()
                self._ensure_open()

            yield page
        finally:
            try:
                if page is not None:
                    try:
                        await page.close()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            f"[browser-runtime] failed to close page: {exc}"
                        )
            finally:
                # Cancellation while waiting, creating, using, or closing a
                # page must never consume a permit permanently.
                if slot_acquired:
                    self._page_slots.release()

    async def close(self) -> None:
        """Close every cached resource; interrupted cleanup can be retried.

        Shutdown intent is visible before this method first awaits. This stops
        new page creation immediately, including creators already waiting on
        the lifecycle lock. Successfully closed resources are removed from the
        retry set; resources still present after cancellation are closed by the
        next call.
        """
        if self._closed:
            return
        self._closing = True
        self._entered = False

        async with self._create_lock:
            if self._closed:
                self._closing = False
                return

            for key, context in list(reversed(self._contexts.items())):
                try:
                    await context.close()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        f"[browser-runtime] failed to close context: {exc}"
                    )
                else:
                    if self._contexts.get(key) is context:
                        self._contexts.pop(key, None)

            for key, browser in list(reversed(self._browsers.items())):
                try:
                    await browser.close()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        f"[browser-runtime] failed to close browser: {exc}"
                    )
                else:
                    if self._browsers.get(key) is browser:
                        self._browsers.pop(key, None)
                    # Closing a browser also closes every context it owns, even
                    # if an earlier explicit context.close() reported an error.
                    for context_key in list(self._contexts):
                        if context_key.browser == key:
                            self._contexts.pop(context_key, None)

            if self._playwright is not None:
                playwright = self._playwright
                try:
                    await playwright.stop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        f"[browser-runtime] failed to stop Playwright: {exc}"
                    )
                else:
                    if self._playwright is playwright:
                        self._playwright = None
                    # stop() owns the underlying driver process and therefore
                    # closes anything whose individual close failed.
                    self._contexts.clear()
                    self._browsers.clear()

            if (
                self._playwright is None
                and not self._contexts
                and not self._browsers
            ):
                self._closed = True
                self._closing = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("BrowserRuntime is closed")
        if self._closing:
            raise RuntimeError("BrowserRuntime is closing")
        if not self._entered:
            raise RuntimeError(
                "BrowserRuntime must be entered with 'async with' before use"
            )

    async def _get_context_locked(self, key: _ContextKey) -> Any:
        context = self._contexts.get(key)
        if context is not None:
            return context

        browser = await self._get_browser_locked(key.browser)
        self._ensure_open()
        kwargs: dict[str, Any] = {"user_agent": USER_AGENT}
        if key.storage_state:
            kwargs["storage_state"] = key.storage_state
            logger.info(f"Using session: {key.storage_state}")
        if key.viewport:
            kwargs["viewport"] = {
                "width": key.viewport[0],
                "height": key.viewport[1],
            }
        context = await browser.new_context(**kwargs)
        # Track before checking shutdown intent so close() can always find a
        # context whose creation completed concurrently with its call.
        self._contexts[key] = context
        self._ensure_open()
        return context

    async def _get_browser_locked(self, key: _BrowserKey) -> Any:
        browser = self._browsers.get(key)
        if browser is not None:
            return browser

        if self._playwright is None:
            self._playwright = await _start_playwright()
            self._ensure_open()

        kwargs: dict[str, Any] = {"headless": key.headless}
        prefer_chrome = key.mode is not BrowserMode.STANDARD
        if key.mode is BrowserMode.STEALTH:
            kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
        elif key.mode is BrowserMode.STEALTH_DIRECT:
            kwargs["args"] = [
                "--disable-blink-features=AutomationControlled",
                "--no-proxy-server",
            ]

        if prefer_chrome:
            try:
                browser = await self._playwright.chromium.launch(
                    channel="chrome", **kwargs
                )
            except Exception:
                self._ensure_open()
                logger.warning(
                    "[browser-runtime] real Chrome unavailable, using bundled "
                    "Chromium (anti-scrape sites may detect it)"
                )
                browser = await self._playwright.chromium.launch(**kwargs)
        else:
            browser = await self._playwright.chromium.launch(**kwargs)

        # Track before checking shutdown intent so a concurrently requested
        # close can retry cleanup even when launch completed after its call.
        self._browsers[key] = browser
        self._ensure_open()
        return browser

    @staticmethod
    def _prepare_storage_state(storage_state: str | Path | None) -> str | None:
        if not storage_state:
            return None
        path = Path(storage_state).expanduser()
        if not path.exists():
            return None

        # Session files contain authentication material and should not be
        # readable by other local users.
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            logger.warning(
                f"Session file {path} has insecure permissions {oct(mode)}. "
                "Should be 0o600. Fixing..."
            )
            os.chmod(path, 0o600)
        return str(path.resolve())


@asynccontextmanager
async def use_browser_runtime(
    runtime: BrowserRuntime | None = None,
) -> AsyncIterator[BrowserRuntime]:
    """Borrow an entered runtime, or create and close one for this operation."""
    if runtime is not None:
        yield runtime
        return
    async with BrowserRuntime() as owned_runtime:
        yield owned_runtime
