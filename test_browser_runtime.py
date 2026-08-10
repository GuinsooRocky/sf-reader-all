"""Offline regression tests for browser runtime reuse and browser fetchers."""

import asyncio
import os
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sf_reader_all.fetchers import browser_runtime as runtime_module
from sf_reader_all.fetchers.browser import fetch_via_browser
from sf_reader_all.fetchers.browser_runtime import BrowserMode, BrowserRuntime
from sf_reader_all.fetchers.twitter import _fetch_via_playwright, fetch_twitter
from sf_reader_all.fetchers.xhs_profile import harvest_profile
from sf_reader_all.reader import UniversalReader
from sf_reader_all.schema import SourceType, UnifiedContent


class _FakePage:
    def __init__(self, *, url="https://example.com/final", content="body text"):
        self.url = url
        self.content = content
        self.goto_calls = []
        self.function_waits = []
        self.selector_waits = []
        self.evaluate_calls = []
        self.closed = False
        self.mouse = _FakeMouse()

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    async def wait_for_function(self, expression, **kwargs):
        self.function_waits.append((expression, kwargs))

    async def wait_for_selector(self, selector, **kwargs):
        self.selector_waits.append((selector, kwargs))

    async def evaluate(self, expression):
        self.evaluate_calls.append(expression)
        if "og:title" in expression:
            return {"title": "DOM article title", "author": "DOM author"}
        return self.content

    async def title(self):
        return "Example title"

    async def inner_text(self, selector):
        return "XHS profile loaded"

    async def close(self):
        self.closed = True


class _FakeMouse:
    def __init__(self):
        self.wheels = []

    async def wheel(self, x, y):
        self.wheels.append((x, y))


class _FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False

    async def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.context_calls = []
        self.contexts = []
        self.closed = False

    async def new_context(self, **kwargs):
        self.context_calls.append(kwargs)
        context = _FakeContext()
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, fail_real_chrome=False):
        self.fail_real_chrome = fail_real_chrome
        self.launch_calls = []
        self.browsers = []

    async def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        if self.fail_real_chrome and kwargs.get("channel") == "chrome":
            raise RuntimeError("real Chrome missing")
        browser = _FakeBrowser()
        self.browsers.append(browser)
        return browser


class _FakePlaywright:
    def __init__(self, fail_real_chrome=False):
        self.chromium = _FakeChromium(fail_real_chrome=fail_real_chrome)
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _BorrowedRuntime:
    def __init__(self, page):
        self.test_page = page
        self.page_calls = []
        self.page_exits = 0

    @asynccontextmanager
    async def page(self, **kwargs):
        self.page_calls.append(kwargs)
        try:
            yield self.test_page
        finally:
            self.page_exits += 1


class _SlowPageRuntime:
    """Borrowed runtime whose page acquisition never completes on its own."""

    def __init__(self):
        self.page_exits = 0

    @asynccontextmanager
    async def page(self, **kwargs):
        try:
            await asyncio.Future()
            yield _FakePage()
        finally:
            self.page_exits += 1


class BrowserRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_and_exit_without_page_never_starts_playwright(self):
        start = AsyncMock()
        with patch.object(runtime_module, "_start_playwright", start):
            async with BrowserRuntime():
                pass
        start.assert_not_awaited()

    async def test_reuses_browser_and_context_and_closes_every_resource(self):
        fake_playwright = _FakePlaywright()
        start = AsyncMock(return_value=fake_playwright)

        with patch.object(runtime_module, "_start_playwright", start):
            async with BrowserRuntime() as runtime:
                async def use_page():
                    async with runtime.page(mode=BrowserMode.STANDARD):
                        await asyncio.sleep(0)

                await asyncio.gather(use_page(), use_page(), use_page())

                self.assertEqual(len(fake_playwright.chromium.launch_calls), 1)
                browser = fake_playwright.chromium.browsers[0]
                self.assertEqual(len(browser.context_calls), 1)
                self.assertEqual(len(browser.contexts[0].pages), 3)
                self.assertTrue(all(page.closed for page in browser.contexts[0].pages))

            self.assertTrue(browser.contexts[0].closed)
            self.assertTrue(browser.closed)
            self.assertTrue(fake_playwright.stopped)
            start.assert_awaited_once()

    async def test_default_page_limit_is_six_and_waiter_cancel_releases_slot(self):
        fake_playwright = _FakePlaywright()
        active = 0
        peak = 0
        six_active = asyncio.Event()
        release = asyncio.Event()

        async def hold_page(runtime):
            nonlocal active, peak
            async with runtime.page():
                active += 1
                peak = max(peak, active)
                if active == 6:
                    six_active.set()
                try:
                    await release.wait()
                finally:
                    active -= 1

        with patch.object(
            runtime_module,
            "_start_playwright",
            AsyncMock(return_value=fake_playwright),
        ):
            async with BrowserRuntime() as runtime:
                holders = [asyncio.create_task(hold_page(runtime)) for _ in range(6)]
                await asyncio.wait_for(six_active.wait(), timeout=1)

                blocked = asyncio.create_task(hold_page(runtime))
                await asyncio.sleep(0)
                self.assertEqual(active, 6)
                self.assertFalse(blocked.done())

                blocked.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await blocked

                release.set()
                await asyncio.gather(*holders)

                async def acquire_after_cancel():
                    async with runtime.page():
                        return True

                self.assertTrue(
                    await asyncio.wait_for(acquire_after_cancel(), timeout=1)
                )

        self.assertEqual(peak, 6)

    async def test_close_during_context_creation_never_yields_or_leaks_context(self):
        fake_playwright = _FakePlaywright()
        context_started = asyncio.Event()
        release_context = asyncio.Event()
        created_contexts = []

        async def slow_new_context(browser, **kwargs):
            context_started.set()
            await release_context.wait()
            context = _FakeContext()
            browser.context_calls.append(kwargs)
            browser.contexts.append(context)
            created_contexts.append(context)
            return context

        with (
            patch.object(
                runtime_module,
                "_start_playwright",
                AsyncMock(return_value=fake_playwright),
            ),
            patch.object(_FakeBrowser, "new_context", slow_new_context),
        ):
            runtime = BrowserRuntime()
            await runtime.__aenter__()
            yielded = False

            async def use_page():
                nonlocal yielded
                async with runtime.page():
                    yielded = True

            page_task = asyncio.create_task(use_page())
            await asyncio.wait_for(context_started.wait(), timeout=1)
            close_task = asyncio.create_task(runtime.close())
            while not runtime._closing:
                await asyncio.sleep(0)
            release_context.set()

            with self.assertRaisesRegex(RuntimeError, "closing"):
                await page_task
            await close_task

        self.assertFalse(yielded)
        self.assertEqual(len(created_contexts), 1)
        self.assertTrue(created_contexts[0].closed)
        self.assertTrue(fake_playwright.chromium.browsers[0].closed)
        self.assertTrue(fake_playwright.stopped)
        self.assertEqual(runtime._contexts, {})

    async def test_close_during_page_creation_never_yields_new_page(self):
        fake_playwright = _FakePlaywright()
        page_started = asyncio.Event()
        release_page = asyncio.Event()
        created_pages = []

        async def slow_new_page(context):
            page_started.set()
            await release_page.wait()
            page = _FakePage()
            context.pages.append(page)
            created_pages.append(page)
            return page

        with (
            patch.object(
                runtime_module,
                "_start_playwright",
                AsyncMock(return_value=fake_playwright),
            ),
            patch.object(_FakeContext, "new_page", slow_new_page),
        ):
            runtime = BrowserRuntime()
            await runtime.__aenter__()
            yielded = False

            async def use_page():
                nonlocal yielded
                async with runtime.page():
                    yielded = True

            page_task = asyncio.create_task(use_page())
            await asyncio.wait_for(page_started.wait(), timeout=1)
            close_task = asyncio.create_task(runtime.close())
            while not runtime._closing:
                await asyncio.sleep(0)
            release_page.set()

            with self.assertRaisesRegex(RuntimeError, "closing"):
                await page_task
            await close_task

        self.assertFalse(yielded)
        self.assertEqual(len(created_pages), 1)
        self.assertTrue(created_pages[0].closed)
        self.assertTrue(fake_playwright.stopped)

    async def test_cancelled_close_can_retry_remaining_cleanup(self):
        fake_playwright = _FakePlaywright()
        with patch.object(
            runtime_module,
            "_start_playwright",
            AsyncMock(return_value=fake_playwright),
        ):
            runtime = BrowserRuntime()
            await runtime.__aenter__()
            async with runtime.page():
                pass

            browser = fake_playwright.chromium.browsers[0]
            context = browser.contexts[0]
            original_close = context.close
            close_started = asyncio.Event()
            attempts = 0

            async def cancel_once():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    close_started.set()
                    await asyncio.Future()
                await original_close()

            context.close = cancel_once
            first_close = asyncio.create_task(runtime.close())
            await asyncio.wait_for(close_started.wait(), timeout=1)
            first_close.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_close

            self.assertFalse(runtime._closed)
            self.assertTrue(runtime._closing)
            self.assertFalse(browser.closed)
            self.assertFalse(fake_playwright.stopped)

            await runtime.close()

        self.assertEqual(attempts, 2)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(fake_playwright.stopped)
        self.assertTrue(runtime._closed)

    async def test_context_key_includes_session_and_viewport(self):
        fake_playwright = _FakePlaywright()
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session.json"
            session.write_text("{}", encoding="utf-8")
            os.chmod(session, 0o644)

            with patch.object(
                runtime_module,
                "_start_playwright",
                AsyncMock(return_value=fake_playwright),
            ):
                async with BrowserRuntime() as runtime:
                    async with runtime.page(
                        mode=BrowserMode.STEALTH,
                        storage_state=session,
                        viewport=(1280, 2000),
                    ):
                        pass
                    async with runtime.page(
                        mode=BrowserMode.STEALTH,
                        storage_state=session,
                        viewport=(1280, 2000),
                    ):
                        pass
                    async with runtime.page(
                        mode=BrowserMode.STEALTH,
                        storage_state=session,
                        viewport=(800, 600),
                    ):
                        pass

                browser = fake_playwright.chromium.browsers[0]
                self.assertEqual(len(fake_playwright.chromium.launch_calls), 1)
                self.assertEqual(len(browser.context_calls), 2)
                self.assertEqual(os.stat(session).st_mode & 0o777, 0o600)

    async def test_stealth_falls_back_to_bundled_chromium(self):
        fake_playwright = _FakePlaywright(fail_real_chrome=True)
        with patch.object(
            runtime_module,
            "_start_playwright",
            AsyncMock(return_value=fake_playwright),
        ):
            async with BrowserRuntime() as runtime:
                async with runtime.page(mode=BrowserMode.STEALTH):
                    pass

        calls = fake_playwright.chromium.launch_calls
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["channel"], "chrome")
        self.assertNotIn("channel", calls[1])

    async def test_unentered_runtime_rejects_page_use(self):
        runtime = BrowserRuntime()
        with self.assertRaisesRegex(RuntimeError, "must be entered"):
            async with runtime.page():
                pass

    async def test_reader_batch_shares_lazy_runtime_without_starting_browser(self):
        reader = UniversalReader()
        seen_runtimes = []

        async def fake_fetch(platform, url, *, browser_runtime=None):
            seen_runtimes.append(browser_runtime)
            return UnifiedContent(
                source_type=SourceType.MANUAL,
                source_name="offline",
                title=url,
                content="body",
                url=url,
            )

        reader._fetch = fake_fetch
        start = AsyncMock()
        with (
            patch.object(runtime_module, "_start_playwright", start),
            patch("sf_reader_all.reader.validate_url"),
            patch("sf_reader_all.utils.storage.save_many_to_markdown"),
        ):
            results = await reader.read_batch(
                ["https://example.com/a", "https://example.com/b"]
            )

        self.assertEqual(len(results), 2)
        self.assertIs(seen_runtimes[0], seen_runtimes[1])
        self.assertIsInstance(seen_runtimes[0], BrowserRuntime)
        start.assert_not_awaited()


class FetcherRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_browser_fetch_borrows_runtime_without_fixed_sleep(self):
        page = _FakePage(content="Ready article")
        runtime = _BorrowedRuntime(page)

        with patch(
            "sf_reader_all.utils.url_validator.validate_url",
            return_value="https://example.com/article",
        ):
            result = await fetch_via_browser(
                "https://example.com/article",
                runtime=runtime,
                timeout_ms=1_000,
            )

        self.assertEqual(result["content"], "Ready article")
        self.assertEqual(runtime.page_calls[0]["mode"], BrowserMode.STANDARD)
        self.assertEqual(runtime.page_exits, 1)
        self.assertEqual(len(page.function_waits), 1)
        self.assertFalse(hasattr(page, "wait_for_timeout"))

    async def test_browser_fetch_enforces_total_deadline_and_releases_page(self):
        page = _FakePage()

        async def never_finishes(*args, **kwargs):
            await asyncio.Future()

        page.goto = never_finishes
        runtime = _BorrowedRuntime(page)

        with patch(
            "sf_reader_all.utils.url_validator.validate_url",
            return_value="https://example.com/slow",
        ):
            with self.assertRaisesRegex(RuntimeError, "total deadline"):
                await fetch_via_browser(
                    "https://example.com/slow",
                    runtime=runtime,
                    timeout_ms=10,
                )
        self.assertEqual(runtime.page_exits, 1)

    async def test_browser_deadline_includes_page_acquisition(self):
        runtime = _SlowPageRuntime()
        with patch(
            "sf_reader_all.utils.url_validator.validate_url",
            return_value="https://example.com/slow-acquisition",
        ):
            with self.assertRaisesRegex(RuntimeError, "total deadline"):
                await fetch_via_browser(
                    "https://example.com/slow-acquisition",
                    runtime=runtime,
                    timeout_ms=10,
                )
        self.assertEqual(runtime.page_exits, 1)

    async def test_owned_runtime_is_stopped_when_acquisition_times_out(self):
        fake_playwright = _FakePlaywright()

        async def hanging_launch(**kwargs):
            await asyncio.Future()

        fake_playwright.chromium.launch = hanging_launch
        with (
            patch.object(
                runtime_module,
                "_start_playwright",
                AsyncMock(return_value=fake_playwright),
            ),
            patch(
                "sf_reader_all.utils.url_validator.validate_url",
                return_value="https://example.com/slow-runtime",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "total deadline"):
                await fetch_via_browser(
                    "https://example.com/slow-runtime",
                    timeout_ms=10,
                )

        self.assertTrue(fake_playwright.stopped)

    async def test_wechat_uses_dom_metadata_when_html_title_is_empty(self):
        page = _FakePage(
            url="https://mp.weixin.qq.com/s/example",
            content="Full WeChat article body",
        )

        async def empty_title():
            return ""

        page.title = empty_title
        runtime = _BorrowedRuntime(page)

        with patch(
            "sf_reader_all.utils.url_validator.validate_url",
            return_value="https://mp.weixin.qq.com/s/example",
        ):
            result = await fetch_via_browser(
                "https://mp.weixin.qq.com/s/example",
                runtime=runtime,
                timeout_ms=1_000,
            )

        self.assertEqual(result["title"], "DOM article title")
        self.assertEqual(result["author"], "DOM author")
        self.assertEqual(result["content"], "Full WeChat article body")

    async def test_twitter_fallback_borrows_stealth_runtime(self):
        page = _FakePage(content="A sufficiently long tweet body for extraction")
        runtime = _BorrowedRuntime(page)

        result = await _fetch_via_playwright(
            "https://x.com/user/status/123",
            runtime=runtime,
            timeout_ms=1_000,
        )

        self.assertIn("tweet body", result["text"])
        self.assertEqual(runtime.page_calls[0]["mode"], BrowserMode.STEALTH)
        self.assertEqual(len(page.function_waits), 1)

    async def test_twitter_deadline_includes_page_acquisition(self):
        runtime = _SlowPageRuntime()
        with self.assertRaisesRegex(RuntimeError, "total deadline"):
            await _fetch_via_playwright(
                "https://x.com/user/status/123",
                runtime=runtime,
                timeout_ms=10,
            )
        self.assertEqual(runtime.page_exits, 1)

    async def test_twitter_http_tier_does_not_block_event_loop(self):
        def slow_fxtwitter(url):
            time.sleep(0.08)
            return {
                "text": "threaded FxTwitter response",
                "title": "threaded FxTwitter response",
                "author": "@user",
            }

        with patch(
            "sf_reader_all.fetchers.twitter._fetch_via_fxtwitter",
            side_effect=slow_fxtwitter,
        ):
            task = asyncio.create_task(
                fetch_twitter("https://x.com/user/status/123")
            )
            await asyncio.sleep(0.01)
            self.assertFalse(
                task.done(),
                "blocking requests tier stalled the event loop until completion",
            )
            result = await task

        self.assertEqual(result["text"], "threaded FxTwitter response")

    async def test_twitter_jina_tier_uses_async_adapter(self):
        jina = AsyncMock(
            return_value={
                "content": "J" * 120,
                "title": "Useful profile",
            }
        )
        with (
            patch(
                "sf_reader_all.fetchers.twitter._fetch_via_fxtwitter",
                side_effect=RuntimeError("tier 1 down"),
            ),
            patch(
                "sf_reader_all.fetchers.twitter._fetch_via_oembed",
                side_effect=RuntimeError("tier 2 down"),
            ),
            patch(
                "sf_reader_all.fetchers.twitter.fetch_via_jina_async",
                jina,
            ),
        ):
            result = await fetch_twitter("https://x.com/user/status/123")

        self.assertEqual(result["text"], "J" * 120)
        jina.assert_awaited_once_with("https://x.com/user/status/123")

    async def test_xhs_profile_borrows_runtime_and_waits_for_new_cards(self):
        page = _FakePage(
            url="https://www.xiaohongshu.com/user/profile/abc",
        )
        note = {
            "id": "abc123",
            "href": (
                "https://www.xiaohongshu.com/user/profile/u/abc123"
                "?xsec_token=token123"
            ),
            "text": "note title",
        }

        async def evaluate(expression):
            page.evaluate_calls.append(expression)
            return [dict(note)]

        page.evaluate = evaluate
        runtime = _BorrowedRuntime(page)

        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "xhs.json"
            session.write_text("{}", encoding="utf-8")
            with patch(
                "sf_reader_all.utils.url_validator.validate_url",
                return_value="https://www.xiaohongshu.com/user/profile/abc",
            ):
                result = await harvest_profile(
                    "https://www.xiaohongshu.com/user/profile/abc",
                    session=str(session),
                    runtime=runtime,
                    max_scrolls=1,
                    stable_rounds=1,
                    scroll_wait_ms=50,
                    timeout_ms=1_000,
                )

        self.assertEqual(result[0]["id"], "abc123")
        self.assertIn("/explore/abc123", result[0]["href"])
        self.assertEqual(runtime.page_calls[0]["viewport"], (1280, 2000))
        self.assertEqual(runtime.page_calls[0]["mode"], BrowserMode.STEALTH)
        self.assertEqual(page.mouse.wheels, [(0, 6000)])
        self.assertEqual(len(page.function_waits), 2)
        self.assertEqual(page.function_waits[1][1]["arg"], ["abc123"])
        self.assertFalse(hasattr(page, "wait_for_timeout"))

    async def test_wechat_browser_fallback_receives_shared_runtime(self):
        from sf_reader_all.fetchers.wechat import fetch_wechat

        runtime = object()
        jina = AsyncMock(
            return_value={
                "title": "Weixin Official Accounts Platform",
                "content": "[去验证]",
            }
        )
        browser = AsyncMock(
            return_value={
                "title": "Article",
                "content": "Full article body",
                "author": "author",
            }
        )
        with (
            patch("sf_reader_all.fetchers.jina.fetch_via_jina_async", jina),
            patch("sf_reader_all.fetchers.browser.fetch_via_browser", browser),
        ):
            result = await fetch_wechat(
                "https://mp.weixin.qq.com/s/example", runtime=runtime
            )

        self.assertEqual(result["content"], "Full article body")
        browser.assert_awaited_once_with(
            "https://mp.weixin.qq.com/s/example",
            stealth=True,
            runtime=runtime,
        )

    async def test_xhs_browser_fallback_receives_shared_runtime(self):
        from sf_reader_all.fetchers.xhs import fetch_xhs

        runtime = object()
        jina = AsyncMock(
            return_value={
                "title": "小红书 - 你的生活兴趣社区",
                "content": "登录后推荐更懂你的笔记",
            }
        )
        browser = AsyncMock(
            return_value={
                "title": "Note",
                "content": "Note body",
                "author": "author",
                "url": "https://www.xiaohongshu.com/explore/example?xsec_token=t",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "xhs.json"
            session.write_text("{}", encoding="utf-8")
            with (
                patch("sf_reader_all.fetchers.xhs.fetch_via_jina_async", jina),
                patch(
                    "sf_reader_all.fetchers.browser.get_session_path",
                    return_value=str(session),
                ),
                patch(
                    "sf_reader_all.fetchers.browser.fetch_via_browser", browser
                ),
            ):
                result = await fetch_xhs(
                    "https://www.xiaohongshu.com/explore/example?xsec_token=t",
                    runtime=runtime,
                )

        self.assertEqual(result["content"], "Note body")
        browser.assert_awaited_once_with(
            "https://www.xiaohongshu.com/explore/example?xsec_token=t",
            storage_state=str(session),
            runtime=runtime,
        )


if __name__ == "__main__":
    unittest.main()
