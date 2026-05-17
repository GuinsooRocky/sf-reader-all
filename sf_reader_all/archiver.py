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
from html import escape as _esc
from pathlib import Path
from urllib.parse import quote, urlparse

from loguru import logger

from sf_reader_all.utils.mhtml import mhtml_to_selfcontained
from sf_reader_all.utils.url_validator import validate_url

SESSION_DIR = Path.home() / ".sf-reader-all" / "sessions"
TIMEOUT_MS = 60_000
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")


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
    validate_url(url)
    from playwright.async_api import async_playwright

    session_path = session_for(session)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
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


async def _capture(page, cdp, url: str, mhtml_path: Path) -> str:
    """Snapshot one page to MHTML; return its <title>."""
    await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(2500)
    title = (await page.title() or "").strip()
    snap = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
    mhtml_path.write_text(snap["data"], encoding="utf-8")
    return title


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
             ("index", "section", "url", "title", "file", "status", "error")}
            for e in entries]
    (Path(out_dir) / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_archive(input_file, out_dir, *, theme="dark", concurrency=5,
                      strip_patterns=(), session=None) -> list[dict]:
    """Snapshot every URL in `input_file` into self-contained HTML.

    Incremental: an entry whose `NNN-*.html` already exists is skipped.
    """
    entries = parse_input(input_file)
    if not entries:
        raise ValueError(f"no URLs found in {input_file}")
    for e in entries:
        validate_url(e["url"])
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

    async def worker(wid: int, context):
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)
        while True:
            try:
                e = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            idx = e["index"]
            try:
                existing = sorted(out.glob(f"{idx:03d}-*.html"))
                if existing:
                    e["file"] = existing[0].name
                    e["status"] = "skip"
                    e["title"] = e["title"] or existing[0].stem.split("-", 1)[-1]
                    logger.info(f"[W{wid}] skip {idx:03d} (exists)")
                    results.append(e)
                    continue
                mhtml_path = mhtml_dir / f"{idx:03d}.mhtml"
                page_title = await _capture(page, cdp, e["url"], mhtml_path)
                e["title"] = e["title"] or page_title or e["url"]
                html_path = out / f"{idx:03d}-{_safe_name(e['title'])}.html"
                mhtml_to_selfcontained(mhtml_path, html_path, theme=theme,
                                       strip_patterns=strip_patterns)
                mhtml_path.unlink(missing_ok=True)
                e["file"] = html_path.name
                e["status"] = "ok"
                logger.info(f"[W{wid}] ok {idx:03d} {e['title'][:50]}")
            except Exception as exc:
                e["status"] = "fail"
                e["error"] = str(exc)
                e["file"] = ""
                logger.error(f"[W{wid}] fail {idx:03d} {e['url']}: {exc}")
            results.append(e)
        await page.close()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_kw = {"user_agent": UA,
                  "viewport": {"width": 1440, "height": 1100}}
        if session_path and Path(session_path).exists():
            ctx_kw["storage_state"] = str(session_path)
            logger.info(f"using session: {session_path}")
        context = await browser.new_context(**ctx_kw)
        await asyncio.gather(*[asyncio.create_task(worker(i + 1, context))
                               for i in range(max(1, concurrency))])
        await context.close()
        await browser.close()

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
