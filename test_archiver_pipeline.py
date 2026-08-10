"""Regression tests for archive capture metrics and MHTML conversion."""

import asyncio
import json
import sys
import tempfile
import threading
import time
import types
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import patch

from sf_reader_all.archiver import (
    _capture,
    _run_conversion_cancellation_safe,
    _write_manifest,
    run_archive,
)
from sf_reader_all.utils.mhtml import mhtml_to_selfcontained


class _FakePage:
    def __init__(self):
        self.stability_checked = False

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_load_state(self, state, **kwargs):
        assert state == "networkidle"

    async def wait_for_function(self, expression, **kwargs):
        self.stability_checked = True

    async def title(self):
        return "Captured page"


class _FakeCdp:
    async def send(self, method, params):
        assert method == "Page.captureSnapshot"
        return {"data": "MIME-Version: 1.0\n\nSnapshot"}


class _ArchivePage(_FakePage):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def goto(self, url, **kwargs):
        self.url = url
        if url.endswith("/slow"):
            await asyncio.Future()

    async def close(self):
        self.closed = True


class _ArchiveContext:
    def __init__(self, *, fail_cdp=False):
        self.fail_cdp = fail_cdp
        self.pages = []
        self.closed = False

    async def new_page(self):
        page = _ArchivePage()
        self.pages.append(page)
        return page

    async def new_cdp_session(self, page):
        if self.fail_cdp:
            raise RuntimeError("CDP setup failed")
        return _FakeCdp()

    async def close(self):
        self.closed = True


class _ArchiveBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    async def new_context(self, **kwargs):
        return self.context

    async def close(self):
        self.closed = True


class _ArchiveChromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, **kwargs):
        return self.browser


class _ArchivePlaywrightManager:
    def __init__(self, browser):
        self.value = types.SimpleNamespace(chromium=_ArchiveChromium(browser))
        self.exited = False

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


def _fake_playwright_modules(manager):
    package = types.ModuleType("playwright")
    package.__path__ = []
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: manager
    return {"playwright": package, "playwright.async_api": async_api}


def test_capture_reports_stage_metrics_without_fixed_sleep():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.mhtml"
        page = _FakePage()
        title, metrics = asyncio.run(
            _capture(page, _FakeCdp(), "https://example.com", path)
        )

        assert title == "Captured page"
        assert page.stability_checked is True
        assert path.read_text(encoding="utf-8").endswith("Snapshot")
        assert metrics["mhtml_bytes"] == path.stat().st_size
        for name in ("navigation_ms", "settle_ms", "snapshot_ms"):
            assert metrics[name] >= 0


def test_capture_marks_tolerated_settle_timeouts():
    async def time_out(*args, **kwargs):
        raise TimeoutError("expected settle timeout")

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.mhtml"
        page = _FakePage()
        page.wait_for_load_state = time_out
        page.wait_for_function = time_out
        _, metrics = asyncio.run(
            _capture(page, _FakeCdp(), "https://example.com", path)
        )

    assert metrics["network_idle_timed_out"] is True
    assert metrics["stable_content_timed_out"] is True


def test_capture_failure_keeps_completed_stage_metrics():
    class FailingCdp:
        async def send(self, method, params):
            raise RuntimeError("snapshot failed")

    with tempfile.TemporaryDirectory() as directory:
        metrics = {}
        try:
            asyncio.run(
                _capture(
                    _FakePage(),
                    FailingCdp(),
                    "https://example.com",
                    Path(directory) / "page.mhtml",
                    metrics=metrics,
                )
            )
        except RuntimeError as error:
            assert str(error) == "snapshot failed"
        else:
            raise AssertionError("snapshot failure was swallowed")

    assert metrics["_stage"] == "snapshot"
    for name in ("navigation_ms", "settle_ms", "snapshot_ms"):
        assert metrics[name] >= 0


def test_mhtml_conversion_and_manifest_keep_provenance_metrics():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        mhtml_path = root / "page.mhtml"
        html_path = root / "page.html"

        message = MIMEMultipart("related")
        html = MIMEText(
            '<html><body><script>remove()</script><h1>Saved</h1></body></html>',
            "html",
            "utf-8",
        )
        html.add_header("Content-Location", "https://example.com/")
        message.attach(html)
        mhtml_path.write_bytes(message.as_bytes())

        size = mhtml_to_selfcontained(mhtml_path, html_path)
        output = html_path.read_text(encoding="utf-8")
        assert size == html_path.stat().st_size
        assert "<h1>Saved</h1>" in output
        assert "remove()" not in output

        entry = {
            "index": 1,
            "section": "",
            "url": "https://example.com/",
            "title": "Saved",
            "file": "001-saved.html",
            "status": "ok",
            "navigation_ms": 10.0,
            "settle_ms": 20.0,
            "snapshot_ms": 30.0,
            "convert_ms": 40.0,
            "convert_queue_ms": 5.0,
            "total_ms": 100.0,
            "mhtml_bytes": mhtml_path.stat().st_size,
            "html_bytes": size,
        }
        _write_manifest(root, [entry])
        saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        assert saved[0]["convert_ms"] == 40.0
        assert saved[0]["html_bytes"] == size


def test_mhtml_publish_is_atomic_when_replace_fails():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        mhtml_path = root / "page.mhtml"
        html_path = root / "page.html"
        html_path.write_text("original complete output", encoding="utf-8")

        message = MIMEMultipart("related")
        html = MIMEText("<html><body>replacement</body></html>", "html", "utf-8")
        html.add_header("Content-Location", "https://example.com/")
        message.attach(html)
        mhtml_path.write_bytes(message.as_bytes())

        with patch(
            "sf_reader_all.utils.mhtml.os.replace",
            side_effect=OSError("replace failed"),
        ):
            try:
                mhtml_to_selfcontained(mhtml_path, html_path)
            except OSError as error:
                assert str(error) == "replace failed"
            else:
                raise AssertionError("atomic replace failure was swallowed")

        assert html_path.read_text(encoding="utf-8") == "original complete output"
        assert list(root.glob(f".{html_path.name}.*.tmp")) == []

        new_path = root / "new-page.html"
        with patch(
            "sf_reader_all.utils.mhtml.os.replace",
            side_effect=OSError("replace failed"),
        ):
            try:
                mhtml_to_selfcontained(mhtml_path, new_path)
            except OSError:
                pass
            else:
                raise AssertionError("atomic replace failure was swallowed")
        assert not new_path.exists()
        assert list(root.glob(f".{new_path.name}.*.tmp")) == []


def test_cancelled_conversion_holds_its_slot_until_thread_finishes():
    started = threading.Event()
    release = threading.Event()

    def blocking_conversion():
        started.set()
        release.wait(timeout=2)
        return 1

    async def scenario():
        slots = asyncio.Semaphore(1)

        async def run_one():
            async with slots:
                return await _run_conversion_cancellation_safe(
                    blocking_conversion
                )

        task = asyncio.create_task(run_one())
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        held_until_thread_finished = slots.locked() and not task.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("conversion cancellation was swallowed")
        return held_until_thread_finished, slots.locked()

    try:
        held, still_locked = asyncio.run(scenario())
    finally:
        release.set()
    assert held is True
    assert still_locked is False


def test_item_timeout_fails_only_slow_page_and_closes_resources():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        input_path = root / "urls.txt"
        input_path.write_text(
            "https://example.com/slow\n"
            "https://example.com/fast\n"
            "https://example.com/slow-convert\n",
            encoding="utf-8",
        )
        context = _ArchiveContext()
        browser = _ArchiveBrowser(context)
        manager = _ArchivePlaywrightManager(browser)

        def convert(mhtml_path, html_path, **kwargs):
            if Path(mhtml_path).stem == "003":
                time.sleep(0.15)
            Path(html_path).write_text("complete", encoding="utf-8")
            return Path(html_path).stat().st_size

        started = time.monotonic()
        with (
            patch.dict(sys.modules, _fake_playwright_modules(manager)),
            patch("sf_reader_all.archiver.validate_url"),
            patch("sf_reader_all.archiver.mhtml_to_selfcontained", convert),
        ):
            results = asyncio.run(
                run_archive(
                    input_path,
                    root / "output",
                    concurrency=3,
                    item_timeout_ms=100,
                )
            )
        elapsed = time.monotonic() - started

        assert [entry["status"] for entry in results] == ["fail", "ok", "fail"]
        assert results[0]["timed_out"] is True
        assert results[0]["timeout_stage"] == "navigation"
        assert results[0]["navigation_ms"] >= 0
        assert results[1]["timed_out"] is False
        assert results[2]["timed_out"] is True
        assert results[2]["timeout_stage"] == "convert"
        assert list((root / "output").glob("003-*.html")) == []
        assert elapsed < 1
        assert context.closed is True
        assert browser.closed is True
        assert manager.exited is True
        assert context.pages and all(page.closed for page in context.pages)

        manifest = json.loads(
            (root / "output" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest[0]["failure_stage"] == "navigation"
        assert manifest[0]["item_timeout_ms"] == 100


def test_worker_setup_failure_still_closes_page_context_and_browser():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        input_path = root / "urls.txt"
        input_path.write_text("https://example.com/page\n", encoding="utf-8")
        context = _ArchiveContext(fail_cdp=True)
        browser = _ArchiveBrowser(context)
        manager = _ArchivePlaywrightManager(browser)

        with (
            patch.dict(sys.modules, _fake_playwright_modules(manager)),
            patch("sf_reader_all.archiver.validate_url"),
        ):
            try:
                asyncio.run(run_archive(input_path, root / "output"))
            except RuntimeError as error:
                assert str(error) == "CDP setup failed"
            else:
                raise AssertionError("worker setup failure was swallowed")

        assert context.pages and context.pages[0].closed is True
        assert context.closed is True
        assert browser.closed is True
        assert manager.exited is True


def test_archive_cancellation_propagates_after_closing_resources():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        input_path = root / "urls.txt"
        input_path.write_text("https://example.com/slow\n", encoding="utf-8")
        context = _ArchiveContext()
        browser = _ArchiveBrowser(context)
        manager = _ArchivePlaywrightManager(browser)

        async def scenario():
            task = asyncio.create_task(
                run_archive(
                    input_path,
                    root / "output",
                    item_timeout_ms=10_000,
                )
            )
            while not context.pages or not hasattr(context.pages[0], "url"):
                await asyncio.sleep(0.001)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return
            raise AssertionError("archive cancellation was swallowed")

        with (
            patch.dict(sys.modules, _fake_playwright_modules(manager)),
            patch("sf_reader_all.archiver.validate_url"),
        ):
            asyncio.run(scenario())

        assert context.pages[0].closed is True
        assert context.closed is True
        assert browser.closed is True
        assert manager.exited is True


if __name__ == "__main__":
    test_capture_reports_stage_metrics_without_fixed_sleep()
    print("PASS: capture reports timing metrics")
    test_capture_marks_tolerated_settle_timeouts()
    print("PASS: capture reports tolerated settle timeouts")
    test_capture_failure_keeps_completed_stage_metrics()
    print("PASS: capture failure retains stage metrics")
    test_mhtml_conversion_and_manifest_keep_provenance_metrics()
    print("PASS: MHTML conversion and manifest retain metrics")
    test_mhtml_publish_is_atomic_when_replace_fails()
    print("PASS: failed atomic publish preserves the previous HTML")
    test_cancelled_conversion_holds_its_slot_until_thread_finishes()
    print("PASS: cancelled conversion keeps its slot until its thread finishes")
    test_item_timeout_fails_only_slow_page_and_closes_resources()
    print("PASS: one item timeout does not block the batch and resources close")
    test_worker_setup_failure_still_closes_page_context_and_browser()
    print("PASS: worker setup failure closes every browser resource")
    test_archive_cancellation_propagates_after_closing_resources()
    print("PASS: archive cancellation propagates after resource cleanup")
    print("ALL PASS")
