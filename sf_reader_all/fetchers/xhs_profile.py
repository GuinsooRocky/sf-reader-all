# -*- coding: utf-8 -*-
"""
Xiaohongshu profile harvester — collect every note link from a user's homepage.

A profile page is harder to scrape than a single note: XHS guards it with the
300017 "安全限制 / 访问链接异常" risk wall, which trips instantly on bundled
headless Chromium. The single-note fetcher (fetchers/browser.py) gets away with
plain headless because note detail pages are lenient; profiles are not.

So this harvester uses the same anti-detection combo that login.py relies on:
real Chrome (channel="chrome") + --disable-blink-features=AutomationControlled,
headless by default. It then scrolls the profile to lazy-load all note cards and
collects each note's /explore/ link — keeping the xsec_token query intact, since
fetching a note without it returns 404.

Output feeds straight back into `sf-reader-all <url1> <url2> ...` for the actual
content (serial / low-concurrency, per the project's anti-scrape red line).

Needs the [browser] extra: pip install "sf-reader-all[browser]"
"""

import re
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

SESSION_DIR = Path.home() / ".sf-reader-all" / "sessions"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# The risk wall text XHS shows when it flags an automated profile visit.
_RISK_MARKERS = ("安全限制", "访问链接异常", "300017")

# Walk note cards directly. Each card has a hidden token-less /explore/ anchor
# plus cover/title anchors whose href is /user/profile/{uid}/{noteid}?xsec_token=...
# — the token-bearing one is what we want (token-less links 404 on fetch).
# The title text lives in the card's .title element, not inside any anchor.
_HARVEST_JS = r"""() => {
    const noteId = (h) =>
        (h.match(/\/user\/profile\/[0-9a-fA-F]+\/([0-9a-fA-F]+)/)
         || h.match(/\/(?:explore|discovery\/item)\/([0-9a-fA-F]+)/) || [])[1];
    const out = [];
    for (const card of document.querySelectorAll('section.note-item')) {
        let href = '', id = '';
        for (const a of card.querySelectorAll('a[href]')) {
            const nid = noteId(a.href);
            if (!nid) continue;
            if (!id) id = nid;
            if (a.href.includes('xsec_token')) { href = a.href; id = nid; break; }
        }
        if (!id) continue;
        const t = card.querySelector('.title');
        const text = (t ? (t.innerText || t.textContent) : '')
            .replace(/\s+/g, ' ').trim();
        out.push({ id, href, text });
    }
    return out;
}"""


async def harvest_profile(
    url: str,
    *,
    headless: bool = True,
    max_scrolls: int = 60,
    scroll_wait_ms: int = 1800,
    stable_rounds: int = 4,
    session: str = None,
) -> list[dict]:
    """Load an XHS profile, scroll it to the bottom, return every note link.

    Args:
        url: A xiaohongshu.com/user/profile/... URL (or xhslink.com short link).
        headless: Run without a visible window. Default True — real Chrome plus
            the anti-automation flag clears the 300017 risk wall headless, so it
            runs in the background without stealing focus. The wall is triggered
            by a stale xsec_token, not by headless; pass a fresh share link (an
            xhslink.com short link redirects to a fresh token). Set False only
            if a risk wall persists.
        max_scrolls: Hard cap on scroll iterations (safety backstop).
        scroll_wait_ms: Pause after each scroll for lazy-loaded cards to render.
        stable_rounds: Stop once this many consecutive scrolls find no new notes.
        session: Session name/path; defaults to the saved xhs session.

    Returns:
        List of {"id", "href", "text"} note links, in document order.

    Raises:
        RuntimeError: risk wall hit, session missing/expired, or Playwright absent.
    """
    from sf_reader_all.utils.url_validator import validate_url
    validate_url(url)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            '  pip install "sf-reader-all[browser]"\n'
            "  playwright install chromium"
        )

    session_path = _resolve_session(session)
    if not session_path or not Path(session_path).exists():
        raise RuntimeError(
            "❌ No saved XHS session found.\n"
            "   Run: sf-reader-all login xhs\n"
            "   Then retry."
        )

    notes: dict[str, dict] = {}

    async with async_playwright() as p:
        # Same anti-detection combo as login.py: real Chrome + no automation flag.
        launch_args = dict(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            browser = await p.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            logger.warning("[XHS-profile] real Chrome unavailable, using bundled Chromium")
            browser = await p.chromium.launch(**launch_args)

        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 2000},
            storage_state=str(session_path),
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(3000)

            body_head = (await page.inner_text("body"))[:300]
            if any(m in body_head for m in _RISK_MARKERS):
                raise RuntimeError(
                    "❌ XHS risk wall (300017 安全限制) hit on this profile.\n"
                    "   Most often the xsec_token is stale — open the profile in "
                    "the XHS app, share → copy a fresh link, and retry.\n"
                    + ("   Or try --headed (a visible real Chrome window) if it "
                       "persists.\n"
                       if headless else
                       "   Even --headed was flagged. Refresh the session "
                       "(sf-reader-all login xhs); if it persists, your IP may "
                       "be rate-limited — wait and retry later.\n")
                )
            if _looks_logged_out(page.url, body_head):
                raise RuntimeError(
                    "❌ XHS session expired (profile shows a login wall).\n"
                    "   Run: sf-reader-all login xhs\n"
                    "   Then retry."
                )

            last, stagnant = 0, 0
            for i in range(max_scrolls):
                batch = await page.evaluate(_HARVEST_JS)
                for b in batch:
                    prev = notes.get(b["id"])
                    if prev is None:
                        notes[b["id"]] = b
                        continue
                    # Fill in whatever the earlier pass was missing.
                    if b["text"] and not prev["text"]:
                        prev["text"] = b["text"]
                    if "xsec_token" in b["href"] and "xsec_token" not in prev["href"]:
                        prev["href"] = b["href"]
                await page.mouse.wheel(0, 6000)
                await page.wait_for_timeout(scroll_wait_ms)
                if len(notes) == last:
                    stagnant += 1
                else:
                    stagnant = 0
                last = len(notes)
                logger.info(f"[XHS-profile] scroll {i + 1}: {len(notes)} notes")
                if stagnant >= stable_rounds:
                    break

            return [_canonicalize(n) for n in notes.values()]
        finally:
            await context.close()
            await browser.close()


def _canonicalize(note: dict) -> dict:
    """Rewrite a harvested link to the /explore/{id}?xsec_token=... form the
    fetcher understands. Profile cards link via /user/profile/{uid}/{noteid},
    which Jina may resolve to the profile instead of the note."""
    from urllib.parse import parse_qs
    href = note["href"]
    token = (parse_qs(urlparse(href).query).get("xsec_token") or [""])[0]
    if token:
        note["href"] = (f"https://www.xiaohongshu.com/explore/{note['id']}"
                        f"?xsec_token={token}&xsec_source=pc_user")
    elif not href:
        note["href"] = f"https://www.xiaohongshu.com/explore/{note['id']}"
    return note


def _resolve_session(session: str):
    """Resolve a session name/path; default to the saved xhs session."""
    if not session:
        return SESSION_DIR / "xhs.json"
    p = Path(session).expanduser()
    if p.suffix == ".json" or p.exists():
        return p
    return SESSION_DIR / f"{session}.json"


def _looks_logged_out(final_url: str, body_head: str) -> bool:
    if "login" in (final_url or ""):
        return True
    return "登录后推荐更懂你的笔记" in body_head
