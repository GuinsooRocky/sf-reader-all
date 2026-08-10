# -*- coding: utf-8 -*-
"""Site-agnostic page archiver.

Two operations, both deliberately dumb so they work on any site:

  harvest_links(url) -- load a page, return every same-origin <a href>.
                        Curating that raw list down to "the pages I
                        actually want" is left to the caller.
  run_archive(...)   -- given a curated URL list, snapshot each page as
                        MHTML, convert to self-contained HTML, write an
                        index. No per-site logic.

Needs the [browser] extra: pip install "sf-reader-all[browser]"
"""

import asyncio
import json
import os
import re
import time
from html import escape as _esc
from pathlib import Path
from urllib.parse import quote, urlparse

from loguru import logger

from sf_reader_all.utils.mhtml import mhtml_to_selfcontained
from sf_reader_all.utils.async_runtime import run_blocking
from sf_reader_all.utils.url_validator import validate_url

SESSION_DIR = Path.home() / ".sf-reader-all" / "sessions"
TIMEOUT_MS = 60_000
ITEM_TIMEOUT_MS = 120_000
RESOURCE_CLOSE_TIMEOUT_SECONDS = 10
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")


def _is_timeout_exception(exc: BaseException) -> bool:
    """Recognize asyncio, builtin, and Playwright timeout exceptions."""
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or (
        exc.__class__.__name__ == "TimeoutError"
    )


async def _close_resource(resource, label: str) -> None:
    """Best-effort bounded close without hiding task cancellation."""
    if resource is None:
        return
    try:
        await asyncio.wait_for(
            resource.close(), timeout=RESOURCE_CLOSE_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning(f"Timed out closing archive {label}")
    except Exception as exc:
        logger.warning(f"Failed to close archive {label}: {exc}")


async def _launch_browser(playwright):
    """Use bundled Chromium when present, otherwise the installed Chrome."""
    try:
        return await playwright.chromium.launch(headless=True)
    except Exception:
        logger.warning(
            "Bundled Chromium unavailable for archive; trying installed Chrome"
        )
        return await playwright.chromium.launch(channel="chrome", headless=True)


async def _run_conversion_cancellation_safe(func, *args, **kwargs):
    """Keep the backing thread alive and awaited if its caller is cancelled.

    ``asyncio.to_thread`` cannot stop work that has already started. Shielding
    and draining its task keeps the caller's conversion semaphore occupied
    until that work really ends, while still propagating cancellation.
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            if task.cancelled():
                raise cancellation
            continue
        except BaseException:
            if cancellation is not None:
                raise cancellation
            raise
        if cancellation is not None:
            raise cancellation
        return result


def session_for(name_or_url):
    """Resolve a --session value (or a URL) to a storage_state path.

    Bare name -> ~/.sf-reader-all/sessions/<name>.json
    URL       -> keyed by domain
    Path      -> used as-is
    """
    if not name_or_url:
        return None
    if name_or_url.startswith(("http://", "https://")):
        return SESSION_DIR / f"{urlparse(name_or_url).netloc}.json"
    p = Path(name_or_url).expanduser()
    if p.suffix == ".json" or p.exists():
        return p
    return SESSION_DIR / f"{name_or_url}.json"


def _safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]', "-", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:110] or "untitled"


# =============================================================================
# Step 1 — link discovery (generic, unfiltered)
# =============================================================================

async def harvest_links(url: str, *, session=None) -> list[dict]:
    """Load `url` in a browser and return same-origin links.

    Returns a list of {"text": ..., "href": ...}, deduped by href, in
    document order. Intentionally unfiltered — it does not try to tell
    article links from navigation chrome; the caller curates.
    """
    await run_blocking(validate_url, url)
    from playwright.async_api import async_playwright

    session_path = session_for(session)
    async with async_playwright() as p:
        browser = await _launch_browser(p)
        ctx_kw = {"user_agent": UA}
        if session_path and Path(session_path).exists():
            ctx_kw["storage_state"] = str(session_path)
        context = await browser.new_context(**ctx_kw)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=TIMEOUT_MS)
            await page.wait_for_timeout(3000)
            links = await page.evaluate(r"""() => {
                const origin = location.origin;
                const seen = new Set();
                const out = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    let abs;
                    try { abs = new URL(a.getAttribute('href'), location.href); }
                    catch (e) { continue; }
                    if (abs.origin !== origin) continue;
                    abs.hash = '';
                    if (seen.has(abs.href)) continue;
                    seen.add(abs.href);
                    const text = (a.innerText || a.textContent || '')
                        .replace(/\s+/g, ' ').trim();
                    out.push({ text, href: abs.href });
                }
                return out;
            }""")
            return links
        finally:
            await context.close()
            await browser.close()


# =============================================================================
# Step 2 — archive a curated URL list
# =============================================================================

def parse_input(input_file) -> list[dict]:
    """Parse a URL list file into [{"section", "url", "title"}].

    Format (one item per line):
        # ...            comment, ignored
        ## Section Name  index section header
        https://...      a page to archive
        https://... | Custom Title
    """
    entries = []
    section = ""
    for raw in Path(input_file).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("# "):
            continue
        if line.startswith("##"):
            section = line.lstrip("#").strip()
            continue
        title = ""
        if " | " in line:
            line, title = (x.strip() for x in line.split(" | ", 1))
        if not line.startswith(("http://", "https://")):
            continue
        entries.append({"section": section, "url": line, "title": title})
    return entries


async def _wait_for_stable_content(page, *, timeout_ms=2500) -> bool:
    """Return whether visible content stabilized before the deadline."""
    try:
        await page.wait_for_function(
            r"""() => {
                const body = document.body;
                if (!body) return false;
                const images = Array.from(document.images || []);
                const key = [
                    (body.innerText || '').length,
                    body.childElementCount,
                    images.length,
                    images.filter(img => img.complete).length,
                ].join(':');
                const now = Date.now();
                if (window.__sfReaderStableKey !== key) {
                    window.__sfReaderStableKey = key;
                    window.__sfReaderStableSince = now;
                    return false;
                }
                return now - (window.__sfReaderStableSince || now) >= 500;
            }""",
            timeout=timeout_ms,
            polling=200,
        )
        return True
    except Exception as exc:
        if not _is_timeout_exception(exc):
            raise
        # Dynamic pages may never become completely still. The caller already
        # has an item deadline, so capture the latest rendered state.
        return False


async def _capture(
    page,
    cdp,
    url: str,
    mhtml_path: Path,
    metrics: dict | None = None,
) -> tuple[str, dict]:
    """Snapshot one page to MHTML and return its title plus stage metrics."""
    metrics = metrics if metrics is not None else {}
    metrics["_stage"] = "navigation"
    started = time.perf_counter()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    finally:
        metrics["navigation_ms"] = round(
            (time.perf_counter() - started) * 1000, 2)

    # SPA pages fetch their real content via XHR after DOMContentLoaded; a fixed
    # delay races that fetch. Wait for the network to go idle, then detect a
    # short stable-content window instead of always sleeping for 2.5 seconds.
    metrics["_stage"] = "settle"
    settle_started = time.perf_counter()
    try:
        metrics["network_idle_timed_out"] = False
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as exc:
            if not _is_timeout_exception(exc):
                raise
            metrics["network_idle_timed_out"] = True
        metrics["stable_content_timed_out"] = not (
            await _wait_for_stable_content(page)
        )
    finally:
        metrics["settle_ms"] = round(
            (time.perf_counter() - settle_started) * 1000, 2)

    metrics["_stage"] = "title"
    title = (await page.title() or "").strip()
    metrics["_stage"] = "snapshot"
    snapshot_started = time.perf_counter()
    try:
        snap = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
        await asyncio.to_thread(
            mhtml_path.write_text, snap["data"], encoding="utf-8"
        )
        metrics["mhtml_bytes"] = mhtml_path.stat().st_size
    finally:
        metrics["snapshot_ms"] = round(
            (time.perf_counter() - snapshot_started) * 1000, 2)
    metrics.pop("_stage", None)
    return title, metrics


def write_index(out_dir, entries, *, theme="dark") -> None:
    """Write index.html grouping entries by their section."""
    dark = theme == "dark"
    bg, fg = ("#0b0b0c", "#e5e7eb") if dark else ("#ffffff", "#1f2328")
    accent = "#2dd4bf" if dark else "#0f766e"
    border = "#27272a" if dark else "#e5e7eb"
    n_ok = sum(1 for e in entries if e.get("status") in ("ok", "skip"))
    n_fail = sum(1 for e in entries if e.get("status") == "fail")

    sections, by_sec = [], {}
    for e in entries:
        sec = e.get("section", "")
        if sec not in by_sec:
            by_sec[sec] = []
            sections.append(sec)
        by_sec[sec].append(e)

    parts = [
        "<!doctype html>", '<html lang="zh-CN">', "<head>",
        '<meta charset="utf-8">',
        f"<title>归档索引 · {n_ok} 篇</title>", "<style>",
        f"body{{background:{bg};color:{fg};font-family:-apple-system,"
        "BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.7;"
        "margin:40px auto;max-width:920px;padding:0 20px}",
        f"a{{color:{accent};text-decoration:none}}"
        "a:hover{text-decoration:underline}",
        f"h1{{font-size:24px}}h2{{margin-top:32px;border-top:1px solid "
        f"{border};padding-top:20px;font-size:17px}}",
        "li{margin:5px 0}.u{color:#71717a;font-size:12px}.fail{color:#f87171}",
        "</style>", "</head>", "<body>", "<h1>归档索引</h1>",
        f'<p class="u">成功 {n_ok} · 失败 {n_fail} · 主题 {theme}</p>',
    ]
    for sec in sections:
        if sec:
            parts.append(f"<h2>{_esc(sec)}</h2>")
        parts.append("<ol>")
        for e in by_sec[sec]:
            title = _esc(e.get("title") or e["url"])
            if e.get("status") == "fail":
                parts.append(f'<li class="fail">{title} — 失败</li>')
            else:
                href = quote(e["file"])  # encode spaces / CJK for file:// links
                parts.append(f'<li><a href="./{href}">{title}</a></li>')
        parts.append("</ol>")
    parts += ["</body>", "</html>"]
    (Path(out_dir) / "index.html").write_text("\n".join(parts),
                                              encoding="utf-8")


def _write_manifest(out_dir, entries) -> None:
    data = [{k: e.get(k) for k in
             ("index", "section", "url", "title", "file", "status", "error",
              "failure_stage", "timed_out", "timeout_stage", "item_timeout_ms",
              "network_idle_timed_out", "stable_content_timed_out",
              "navigation_ms", "settle_ms", "snapshot_ms", "convert_ms",
              "convert_queue_ms", "total_ms", "mhtml_bytes", "html_bytes")}
            for e in entries]
    (Path(out_dir) / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _archive_item(
    page,
    cdp,
    entry: dict,
    *,
    out: Path,
    mhtml_dir: Path,
    convert_slots: asyncio.Semaphore,
    theme: str,
    strip_patterns,
) -> None:
    """Capture and convert one entry; the worker owns its total deadline."""
    idx = entry["index"]
    mhtml_path = mhtml_dir / f"{idx:03d}.mhtml"
    capture_metrics: dict = {}
    try:
        page_title, _ = await _capture(
            page,
            cdp,
            entry["url"],
            mhtml_path,
            metrics=capture_metrics,
        )
    finally:
        entry.update(capture_metrics)

    entry["title"] = entry["title"] or page_title or entry["url"]
    html_path = out / f"{idx:03d}-{_safe_name(entry['title'])}.html"
    entry["_stage"] = "convert_queue"
    queue_started = time.perf_counter()
    try:
        async with convert_slots:
            entry["convert_queue_ms"] = round(
                (time.perf_counter() - queue_started) * 1000, 2)
            entry["_stage"] = "convert"
            convert_started = time.perf_counter()
            try:
                entry["html_bytes"] = (
                    await _run_conversion_cancellation_safe(
                        mhtml_to_selfcontained,
                        mhtml_path,
                        html_path,
                        theme=theme,
                        strip_patterns=strip_patterns,
                    )
                )
            except asyncio.CancelledError:
                # The shielded thread has finished before cancellation is
                # propagated, so remove its now-unwanted complete output.
                try:
                    html_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        f"Failed to remove cancelled conversion output "
                        f"{html_path}: {exc}"
                    )
                raise
            finally:
                entry["convert_ms"] = round(
                    (time.perf_counter() - convert_started) * 1000, 2)
    finally:
        if "convert_queue_ms" not in entry:
            entry["convert_queue_ms"] = round(
                (time.perf_counter() - queue_started) * 1000, 2)

    mhtml_path.unlink(missing_ok=True)
    entry["file"] = html_path.name
    entry["status"] = "ok"
    entry.pop("_stage", None)


async def run_archive(input_file, out_dir, *, theme="dark", concurrency=5,
                      strip_patterns=(), session=None,
                      item_timeout_ms=ITEM_TIMEOUT_MS) -> list[dict]:
    """Snapshot every URL in `input_file` into self-contained HTML.

    Incremental: an entry whose `NNN-*.html` already exists is skipped.
    """
    entries = parse_input(input_file)
    if not entries:
        raise ValueError(f"no URLs found in {input_file}")
    if item_timeout_ms <= 0:
        raise ValueError("item_timeout_ms must be greater than zero")
    await asyncio.gather(*(
        run_blocking(validate_url, entry["url"])
        for entry in entries
    ))
    for i, e in enumerate(entries, 1):
        e["index"] = i

    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    mhtml_dir = out / "_mhtml"
    mhtml_dir.mkdir(exist_ok=True)

    session_path = (session_for(session) if session
                    else session_for(entries[0]["url"]))

    from playwright.async_api import async_playwright

    queue: asyncio.Queue = asyncio.Queue()
    for e in entries:
        queue.put_nowait(e)
    results: list[dict] = []
    convert_slots = asyncio.Semaphore(max(1, min(2, concurrency)))
    existing_by_index = {}
    for html_path in out.glob("[0-9][0-9][0-9]-*.html"):
        try:
            index = int(html_path.name[:3])
        except ValueError:
            continue
        previous = existing_by_index.get(index)
        if previous is None or html_path.name < previous.name:
            existing_by_index[index] = html_path

    async def worker(wid: int, context):
        page = None
        try:
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            while True:
                try:
                    e = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                idx = e["index"]
                item_started = time.perf_counter()
                existing = existing_by_index.get(idx)
                if existing:
                    e["file"] = existing.name
                    e["status"] = "skip"
                    e["title"] = e["title"] or existing.stem.split("-", 1)[-1]
                    e["timed_out"] = False
                    e["total_ms"] = round(
                        (time.perf_counter() - item_started) * 1000, 2)
                    logger.info(f"[W{wid}] skip {idx:03d} (exists)")
                    results.append(e)
                    continue

                e["item_timeout_ms"] = item_timeout_ms
                e["timed_out"] = False
                try:
                    await asyncio.wait_for(
                        _archive_item(
                            page,
                            cdp,
                            e,
                            out=out,
                            mhtml_dir=mhtml_dir,
                            convert_slots=convert_slots,
                            theme=theme,
                            strip_patterns=strip_patterns,
                        ),
                        timeout=item_timeout_ms / 1000,
                    )
                    logger.info(
                        f"[W{wid}] ok {idx:03d} {e['title'][:50]} "
                        f"(nav={e['navigation_ms']}ms settle={e['settle_ms']}ms "
                        f"convert={e['convert_ms']}ms)"
                    )
                except asyncio.TimeoutError:
                    stage = e.get("_stage", "unknown")
                    e["status"] = "fail"
                    e["timed_out"] = True
                    e["timeout_stage"] = stage
                    e["failure_stage"] = stage
                    e["error"] = (
                        f"item exceeded {item_timeout_ms} ms deadline "
                        f"during {stage}"
                    )
                    e["file"] = ""
                    logger.error(
                        f"[W{wid}] timeout {idx:03d} {e['url']}: {e['error']}"
                    )
                except Exception as exc:
                    stage = e.get("_stage", "unknown")
                    e["status"] = "fail"
                    e["failure_stage"] = stage
                    if _is_timeout_exception(exc):
                        e["timed_out"] = True
                        e["timeout_stage"] = stage
                    e["error"] = str(exc)
                    e["file"] = ""
                    logger.error(f"[W{wid}] fail {idx:03d} {e['url']}: {exc}")
                finally:
                    e["total_ms"] = round(
                        (time.perf_counter() - item_started) * 1000, 2)
                    e.pop("_stage", None)
                results.append(e)
        finally:
            await _close_resource(page, f"worker {wid} page")

    async with async_playwright() as p:
        browser = None
        context = None
        worker_tasks = []
        try:
            browser = await _launch_browser(p)
            ctx_kw = {"user_agent": UA,
                      "viewport": {"width": 1440, "height": 1100}}
            if session_path and Path(session_path).exists():
                ctx_kw["storage_state"] = str(session_path)
                logger.info(f"using session: {session_path}")
            context = await browser.new_context(**ctx_kw)
            worker_tasks = [
                asyncio.create_task(worker(i + 1, context))
                for i in range(max(1, concurrency))
            ]
            try:
                await asyncio.gather(*worker_tasks)
            finally:
                for task in worker_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
        finally:
            try:
                await _close_resource(context, "context")
            finally:
                await _close_resource(browser, "browser")

    results.sort(key=lambda e: e["index"])
    write_index(out, results, theme=theme)
    _write_manifest(out, results)
    try:
        mhtml_dir.rmdir()  # only succeeds if empty
    except OSError:
        pass
    return results


# =============================================================================
# Login — visible browser, save session keyed by domain
# =============================================================================

def archive_login(url: str) -> None:
    """Open a visible browser at `url`, wait for the user to log in /
    unlock the content, then save the session keyed by the URL domain."""
    validate_url(url)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('❌ Playwright not installed. Run:\n'
              '   pip install "sf-reader-all[browser]"\n'
              "   playwright install chromium")
        return

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_path = SESSION_DIR / f"{urlparse(url).netloc}.json"

    print(f"🌐 Opening {url}")
    print("   Log in / unlock the content in the browser window,")
    print("   then close the window to save the session.\n")

    with sync_playwright() as p:
        launch = dict(headless=False,
                      args=["--disable-blink-features=AutomationControlled"])
        try:
            browser = p.chromium.launch(channel="chrome", **launch)
        except Exception:
            browser = p.chromium.launch(**launch)
        context = browser.new_context(user_agent=UA)
        page = context.new_page()
        page.goto(url)
        try:
            page.wait_for_event("close", timeout=600_000)
        except Exception:
            pass
        context.storage_state(path=str(session_path))
        os.chmod(session_path, 0o600)
        print(f"\n✅ Session saved: {session_path}")
        context.close()
        browser.close()
