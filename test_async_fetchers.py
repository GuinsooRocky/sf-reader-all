"""Offline regression tests for blocking work inside async fetchers."""

import asyncio
import threading
import time
from unittest.mock import patch

from sf_reader_all.fetchers.bilibili import fetch_bilibili
from sf_reader_all.fetchers.rss import RSS_TIMEOUT, fetch_rss
from sf_reader_all.fetchers.youtube import fetch_youtube
from sf_reader_all.reader import ACTIVE_READ_CONCURRENCY, UniversalReader
from sf_reader_all.schema import SourceType, UnifiedContent
from sf_reader_all.utils.async_runtime import (
    BLOCKING_IO_CONCURRENCY,
    run_blocking,
)


class _BilibiliResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "code": 0,
            "data": {
                "title": "offline fixture",
                "owner": {"name": "tester"},
                "stat": {"view": 1},
            },
        }


class _RssResponse:
    content = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>offline feed</title>
    <item><title>offline item</title><link>https://example.test/item</link>
    </item></channel></rss>"""

    def raise_for_status(self):
        return None


def test_bilibili_requests_run_concurrently_without_network():
    """Four 200ms HTTP calls should overlap instead of taking about 800ms."""
    delay = 0.2
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def slow_get(*args, **kwargs):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(delay)
            return _BilibiliResponse()
        finally:
            with lock:
                active -= 1

    async def fetch_all():
        return await asyncio.gather(
            *(fetch_bilibili(f"BV{i:010d}") for i in range(4))
        )

    started_at = time.monotonic()
    with patch("sf_reader_all.fetchers.bilibili.requests.get", side_effect=slow_get):
        results = asyncio.run(fetch_all())
    elapsed = time.monotonic() - started_at

    assert len(results) == 4
    assert peak_active >= 2, f"blocking requests ran serially (peak={peak_active})"
    assert elapsed < 0.65, f"blocking requests took {elapsed:.3f}s; expected overlap"


def test_rss_parser_runs_concurrently_without_network():
    """Requests has a timeout and feedparser only receives downloaded bytes."""
    delay = 0.15
    timeouts = []

    def slow_get(url, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        time.sleep(delay)
        return _RssResponse()

    async def fetch_all():
        return await asyncio.gather(
            *(fetch_rss(f"https://example.test/{i}.xml", limit=1) for i in range(3))
        )

    started_at = time.monotonic()
    with patch("sf_reader_all.fetchers.rss.requests.get", side_effect=slow_get):
        results = asyncio.run(fetch_all())
    elapsed = time.monotonic() - started_at

    assert [items[0]["source"] for items in results] == ["offline feed"] * 3
    assert timeouts == [RSS_TIMEOUT] * 3
    assert elapsed < 0.38, f"feed parsing took {elapsed:.3f}s; expected overlap"


def test_platform_detection_rejects_hostname_spoofs():
    reader = UniversalReader()

    assert reader._detect_platform("https://www.youtube.com/watch?v=1") == "youtube"
    assert reader._detect_platform("https://subdomain.bilibili.com/video/BV1") == "bilibili"
    assert reader._detect_platform("https://mp.weixin.qq.com/s/real") == "wechat"

    assert reader._detect_platform("https://youtube.com.evil.test/watch") == "generic"
    assert reader._detect_platform("https://mp.weixin.qq.com@evil.test/s/x") == "generic"
    assert reader._detect_platform("https://notx.com/user/status/1") == "generic"


def test_dns_validation_runs_without_blocking_event_loop():
    def slow_validate(url):
        time.sleep(0.08)
        return url

    async def scenario():
        reader = UniversalReader()

        async def fake_fetch(platform, url, *, browser_runtime=None):
            return UnifiedContent(
                source_type=SourceType.MANUAL,
                source_name="offline",
                title="validated",
                content="body",
                url=url,
            )

        reader._fetch = fake_fetch
        task = asyncio.create_task(
            reader._read_url("https://example.test/item", persist=False)
        )
        await asyncio.sleep(0.01)
        assert not task.done(), "DNS validation blocked the event loop"
        return await task

    with patch("sf_reader_all.reader.validate_url", side_effect=slow_validate):
        result = asyncio.run(scenario())
    assert result.title == "validated"


def test_reader_batch_propagates_child_cancellation():
    async def scenario():
        reader = UniversalReader()

        async def cancelled_fetch(platform, url, *, browser_runtime=None):
            raise asyncio.CancelledError("child cancelled")

        reader._fetch = cancelled_fetch
        await reader.read_batch(["https://example.test/cancelled"])

    with patch("sf_reader_all.reader.validate_url", return_value=None):
        try:
            asyncio.run(scenario())
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("read_batch returned cancellation as content")


def test_reader_batch_limits_active_reads_and_preserves_order():
    active = 0
    peak_active = 0
    sources = [f"source-{index:02d}" for index in range(ACTIVE_READ_CONCURRENCY + 9)]

    async def scenario():
        nonlocal active, peak_active
        reader = UniversalReader()

        async def fake_read(source, *, persist, browser_runtime=None):
            nonlocal active, peak_active
            assert persist is False
            active += 1
            peak_active = max(peak_active, active)
            try:
                index = int(source.rsplit("-", 1)[-1])
                await asyncio.sleep((len(sources) - index) * 0.001)
                return UnifiedContent(
                    source_type=SourceType.MANUAL,
                    source_name="offline",
                    title=source,
                    content="body",
                    url=f"manual://{source}",
                )
            finally:
                active -= 1

        reader._read_source = fake_read
        return await reader.read_sources(sources)

    with patch("sf_reader_all.utils.storage.save_many_to_markdown"):
        results = asyncio.run(scenario())

    assert peak_active == ACTIVE_READ_CONCURRENCY
    assert [item.title for item in results] == sources


def test_youtube_subprocess_work_runs_concurrently_without_network():
    """Slow yt-dlp work must not serialize otherwise independent videos."""
    delay = 0.15

    async def fake_jina(url):
        return {"title": url, "content": "page description", "author": ""}

    def slow_subtitles(url, lang="en"):
        time.sleep(delay)
        return "offline transcript"

    async def fetch_all():
        return await asyncio.gather(
            *(
                fetch_youtube(f"https://youtu.be/abcdefghij{i}")
                for i in range(3)
            )
        )

    started_at = time.monotonic()
    with (
        patch("sf_reader_all.fetchers.youtube.fetch_via_jina_async", side_effect=fake_jina),
        patch("sf_reader_all.fetchers.youtube._get_subtitles_via_ytdlp", side_effect=slow_subtitles),
    ):
        results = asyncio.run(fetch_all())
    elapsed = time.monotonic() - started_at

    assert all(result["has_transcript"] for result in results)
    assert elapsed < 0.38, f"yt-dlp work took {elapsed:.3f}s; expected overlap"


def test_blocking_runtime_enforces_global_limit():
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def measured_work():
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(0.08)
        finally:
            with lock:
                active -= 1

    async def run_all():
        await asyncio.gather(
            *(run_blocking(measured_work) for _ in range(BLOCKING_IO_CONCURRENCY + 4))
        )

    asyncio.run(run_all())
    assert 2 <= peak_active <= BLOCKING_IO_CONCURRENCY


def test_blocking_runtime_propagates_exception_and_cancellation():
    def fail():
        raise LookupError("original failure")

    try:
        asyncio.run(run_blocking(fail))
    except LookupError as error:
        assert str(error) == "original failure"
    else:
        raise AssertionError("run_blocking swallowed the worker exception")

    started = threading.Event()
    release = threading.Event()

    def wait_until_released():
        started.set()
        release.wait(timeout=2)

    async def cancel_running_work():
        task = asyncio.create_task(run_blocking(wait_until_released))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("run_blocking swallowed task cancellation")

    try:
        asyncio.run(cancel_running_work())
    finally:
        release.set()


if __name__ == "__main__":
    test_bilibili_requests_run_concurrently_without_network()
    print("PASS: Bilibili requests overlap")
    test_rss_parser_runs_concurrently_without_network()
    print("PASS: RSS parsing overlaps")
    test_platform_detection_rejects_hostname_spoofs()
    print("PASS: platform detection rejects hostname spoofs")
    test_dns_validation_runs_without_blocking_event_loop()
    print("PASS: DNS validation stays off the event loop")
    test_reader_batch_propagates_child_cancellation()
    print("PASS: batch child cancellation propagates")
    test_reader_batch_limits_active_reads_and_preserves_order()
    print("PASS: batch active-read limit preserves result order")
    test_youtube_subprocess_work_runs_concurrently_without_network()
    print("PASS: YouTube subprocess work overlaps")
    test_blocking_runtime_enforces_global_limit()
    print("PASS: blocking I/O concurrency is bounded")
    test_blocking_runtime_propagates_exception_and_cancellation()
    print("PASS: exceptions and cancellations propagate")
    print("ALL PASS")
