# -*- coding: utf-8 -*-
"""
Xiaohongshu keyword search — collect note links for a search term.

Calls the web search API (edith.xiaohongshu.com/api/sns/web/v1/search/notes)
directly with the saved login cookies. Request signing (x-s / x-t / x-s-common)
comes from the MIT-licensed `xhshow` library; nothing here is vendored from
MediaCrawler.

Output shape matches xhs-profile ({id, href, text, ...}) so the links feed
straight back into `sf-reader-all <url> ...` — keep that serial.

Needs the [xhs] extra: pip install "sf-reader-all[xhs]"
Session: sf-reader-all login xhs
"""

import json
import random
import time
from pathlib import Path

import requests
from loguru import logger

SESSION_DIR = Path.home() / ".sf-reader-all" / "sessions"

API_HOST = "https://edith.xiaohongshu.com"
SEARCH_URI = "/api/sns/web/v1/search/notes"
PAGE_SIZE = 20
MAX_LIMIT = 200  # small-batch personal use only; bigger runs trip risk control

SORTS = {"general": "general", "hot": "popularity_descending", "latest": "time_descending"}
NOTE_TYPES = {"all": 0, "video": 1, "image": 2}

_BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://www.xiaohongshu.com",
    "referer": "https://www.xiaohongshu.com/",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"),
}

_LOGIN_HINT = "   Run: sf-reader-all login xhs\n   Then retry."


def search_notes(
    keyword: str,
    *,
    limit: int = PAGE_SIZE,
    sort: str = "general",
    note_type: str = "all",
    session: str | None = None,
    page_delay: tuple[float, float] = (1.5, 3.0),
) -> list[dict]:
    """Search XHS notes by keyword and return up to `limit` note links."""
    if sort not in SORTS:
        raise ValueError(f"--sort must be one of {', '.join(SORTS)}")
    if note_type not in NOTE_TYPES:
        raise ValueError(f"--type must be one of {', '.join(NOTE_TYPES)}")
    limit = max(1, min(limit, MAX_LIMIT))

    try:
        from xhshow import Xhshow
    except ImportError:
        raise RuntimeError('❌ xhshow not installed: pip install "sf-reader-all[xhs]"')

    cookies = _load_cookies(_resolve_session(session))
    signer = Xhshow()
    search_id = signer.get_search_id()
    notes: dict[str, dict] = {}

    page = 1
    while len(notes) < limit:
        payload = {
            "keyword": keyword,
            "page": page,
            "page_size": PAGE_SIZE,
            "search_id": search_id,
            "sort": SORTS[sort],
            "note_type": NOTE_TYPES[note_type],
        }
        data = _post(signer, cookies, SEARCH_URI, payload)
        items = data.get("items") or []
        new = 0
        for item in items:
            note = _to_note(item)
            if note and note["id"] not in notes:
                notes[note["id"]] = note
                new += 1
        logger.info(f"[XHS-search] page {page}: +{new}, total {len(notes)}")
        if not data.get("has_more") or not items:
            break
        page += 1
        time.sleep(random.uniform(*page_delay))

    return list(notes.values())[:limit]


def _post(signer, cookies: dict, uri: str, payload: dict) -> dict:
    headers = {
        **_BASE_HEADERS,
        "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        **signer.sign_headers_post(uri=uri, cookies=cookies, payload=payload),
    }
    # Body must be byte-identical to what was signed.
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    resp = requests.post(API_HOST + uri, data=body, headers=headers, timeout=20)

    if resp.status_code in (461, 471):
        raise RuntimeError(
            "❌ XHS demanded a CAPTCHA. Open xiaohongshu.com in your browser, "
            "pass the check, then `sf-reader-all login xhs` and retry later.")
    if resp.status_code in (401, 403, 429):
        raise RuntimeError(f"❌ XHS blocked the request (HTTP {resp.status_code}). Wait and retry later.")

    try:
        body_json = resp.json()
    except ValueError:
        raise RuntimeError(f"❌ XHS returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")

    if body_json.get("success"):
        return body_json.get("data") or {}

    code = body_json.get("code")
    msg = body_json.get("msg") or ""
    if code == -100 or "登录" in msg:
        raise RuntimeError(f"❌ XHS session expired ({msg or code}).\n{_LOGIN_HINT}")
    if code == 300012:
        raise RuntimeError("❌ XHS flagged this IP (300012). Wait a while before retrying.")
    if code == 300011:
        raise RuntimeError("❌ XHS restricted this account (300011). Stop and check the account in the app.")
    raise RuntimeError(f"❌ XHS search failed: code={code} msg={msg}")


def _to_note(item: dict) -> dict | None:
    """Keep real notes; drop recommended/hot query rows."""
    if item.get("model_type") != "note" or not item.get("id"):
        return None
    card = item.get("note_card") or {}
    token = item.get("xsec_token") or ""
    href = f"https://www.xiaohongshu.com/explore/{item['id']}"
    if token:
        href += f"?xsec_token={token}&xsec_source=pc_search"
    return {
        "id": item["id"],
        "href": href,
        "text": (card.get("display_title") or "").strip(),
        "author": (card.get("user") or {}).get("nickname", ""),
        "likes": (card.get("interact_info") or {}).get("liked_count", ""),
        "type": card.get("type", ""),
    }


def _resolve_session(session: str | None) -> Path:
    if not session:
        return SESSION_DIR / "xhs.json"
    p = Path(session).expanduser()
    if p.suffix == ".json" or p.exists():
        return p
    return SESSION_DIR / f"{session}.json"


def _load_cookies(session_path: Path) -> dict:
    if not session_path.exists():
        raise RuntimeError(f"❌ No saved XHS session at {session_path}.\n{_LOGIN_HINT}")
    state = json.loads(session_path.read_text())
    cookies = {c["name"]: c["value"] for c in state.get("cookies", [])
               if "xiaohongshu.com" in c.get("domain", "")}
    if "web_session" not in cookies or "a1" not in cookies:
        raise RuntimeError(f"❌ Saved XHS session has no login cookies.\n{_LOGIN_HINT}")
    return cookies
