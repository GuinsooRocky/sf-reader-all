# -*- coding: utf-8 -*-
"""
sf-reader-all CLI — fetch content from any platform.

Usage:
    sf-reader-all <url>                     # Fetch a single URL
    sf-reader-all <url1> <url2> ...         # Fetch multiple URLs
    sf-reader-all archive-links <url>       # Harvest same-origin links from a page
    sf-reader-all archive <urls-file>       # Snapshot a URL list into self-contained HTML
    sf-reader-all list                      # Show inbox contents
    sf-reader-all clear                     # Clear inbox
"""

import sys
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from sf_reader_all.reader import UniversalReader
from sf_reader_all.schema import UnifiedInbox, SourceType


def get_inbox_path() -> str:
    import os
    return os.getenv("INBOX_FILE", "unified_inbox.json")


def cmd_fetch(urls: list[str], as_json: bool = False):
    """Fetch one or more URLs.

    as_json: machine interface — print fetched items as a JSON array on
    stdout (human logs go to stderr), so callers don't have to locate and
    parse the inbox file. Items are still archived to the inbox as usual.
    """
    inbox = UnifiedInbox(get_inbox_path())
    reader = UniversalReader(inbox=inbox)
    log = sys.stderr if as_json else sys.stdout

    async def run():
        if len(urls) == 1:
            items = [await reader.read(urls[0])]
        else:
            items = await reader.read_batch(urls)

        for item in items:
            print(f"✅ [{item.source_type.value}] {item.title[:60]}", file=log)
        if len(urls) > 1:
            print(f"\n📦 Fetched {len(items)}/{len(urls)} URLs", file=log)

        if as_json:
            print(json.dumps([item.to_dict() for item in items], ensure_ascii=False))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n⏹ Cancelled", file=log)
    except Exception as e:
        print(f"❌ {e}", file=log)
        sys.exit(1)


def cmd_list():
    """Show inbox contents."""
    inbox = UnifiedInbox(get_inbox_path())
    if not inbox.items:
        print("📦 Inbox is empty")
        return

    print(f"📦 Inbox: {len(inbox.items)} items\n")

    emoji_map = {
        SourceType.TELEGRAM: "📢", SourceType.RSS: "📰",
        SourceType.BILIBILI: "🎬", SourceType.XIAOHONGSHU: "📕",
        SourceType.TWITTER: "🐦", SourceType.WECHAT: "💬",
        SourceType.YOUTUBE: "▶️", SourceType.MANUAL: "✏️",
    }

    for i, item in enumerate(inbox.items[-20:], 1):
        emoji = emoji_map.get(item.source_type, "📄")
        print(f"  {i:2d}. {emoji} [{item.source_type.value:8s}] {item.title[:50]}")


def cmd_clear():
    """Clear inbox."""
    path = Path(get_inbox_path())
    if path.exists():
        confirm = input("Clear inbox? (y/N) ")
        if confirm.lower() == 'y':
            path.write_text("[]")
            print("✅ Inbox cleared")
    else:
        print("📦 Inbox is already empty")


def cmd_login(platform: str, headless: bool = False):
    """Open browser for manual login to a platform."""
    from sf_reader_all.login import login
    login(platform, headless=headless)


def _opt(args: list[str], flag: str, default=None):
    """Read a `--flag value` option from an arg list."""
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def cmd_archive_links(args: list[str]):
    """Harvest every same-origin link from a page (raw, unfiltered)."""
    urls = [a for a in args if a.startswith(("http://", "https://"))]
    if not urls:
        print("❌ Usage: sf-reader-all archive-links <url> [--session NAME] [--out FILE]")
        sys.exit(1)

    from sf_reader_all.archiver import harvest_links

    try:
        links = asyncio.run(harvest_links(urls[0], session=_opt(args, "--session")))
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    lines = [f"{l['href']} | {l['text']}" for l in links]
    out_file = _opt(args, "--out")
    if out_file:
        Path(out_file).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"✅ {len(links)} links → {out_file}")
    else:
        print("\n".join(lines))
        print(f"\n📦 {len(links)} same-origin links", file=sys.stderr)


def cmd_archive(args: list[str]):
    """Snapshot a curated URL list into self-contained HTML."""
    login_url = _opt(args, "--login")
    if login_url:
        from sf_reader_all.archiver import archive_login
        archive_login(login_url)
        return

    if not args or args[0].startswith("-"):
        print("❌ Usage: sf-reader-all archive <urls-file> "
              "[--out DIR] [--theme dark|light] [--concurrency N] "
              "[--session NAME] [--strip-pattern REGEX]")
        print("          sf-reader-all archive --login <url>")
        sys.exit(1)

    input_file = args[0]
    out_dir = _opt(args, "--out") or f"{Path(input_file).stem}-archive"
    strip = [args[i + 1] for i, a in enumerate(args)
             if a == "--strip-pattern" and i + 1 < len(args)]

    from sf_reader_all.archiver import run_archive

    try:
        results = asyncio.run(run_archive(
            input_file, out_dir,
            theme=_opt(args, "--theme", "dark"),
            concurrency=int(_opt(args, "--concurrency", "5")),
            strip_patterns=strip,
            session=_opt(args, "--session"),
        ))
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    n_ok = sum(1 for e in results if e.get("status") in ("ok", "skip"))
    n_fail = sum(1 for e in results if e.get("status") == "fail")
    print(f"\n📦 Archived {n_ok}/{len(results)} ({n_fail} failed)")
    print(f"   → {out_dir}/index.html")


def main():
    if len(sys.argv) < 2:
        print("""
📖 sf-reader-all — Universal content reader

Usage:
    sf-reader-all <url>              Fetch content from any URL
    sf-reader-all <url1> <url2>      Fetch multiple URLs
    sf-reader-all login <platform>   Login to a platform (saves session for browser fallback)
    sf-reader-all archive-links <url>   Harvest same-origin links from a page
    sf-reader-all archive <urls-file>   Snapshot a curated URL list into self-contained HTML
    sf-reader-all list               Show inbox contents
    sf-reader-all clear              Clear inbox

Supported platforms:
    WeChat, Telegram, X/Twitter, YouTube,
    Bilibili, Xiaohongshu, RSS, and any web page

Examples:
    sf-reader-all https://mp.weixin.qq.com/s/abc123
    sf-reader-all https://x.com/elonmusk/status/123456
    sf-reader-all https://www.xiaohongshu.com/explore/abc123
    sf-reader-all login xhs
""")
        return

    cmd = sys.argv[1].lower()

    if cmd == "login":
        if len(sys.argv) < 3:
            print("❌ Usage: sf-reader-all login <platform> [--headless]")
            print("   Supported: xhs, wechat")
            sys.exit(1)
        headless = "--headless" in sys.argv
        cmd_login(sys.argv[2], headless=headless)
    elif cmd == "archive-links":
        cmd_archive_links(sys.argv[2:])
    elif cmd == "archive":
        cmd_archive(sys.argv[2:])
    elif cmd == "list":
        cmd_list()
    elif cmd == "clear":
        cmd_clear()
    elif cmd == "--json" or cmd.startswith("http") or cmd.startswith("www.") or "." in cmd:
        args = sys.argv[1:]
        as_json = "--json" in args
        urls = [a for a in args
                if not a.startswith("--") and (a.startswith(("http", "www.")) or "." in a)]
        if not urls:
            print("❌ Usage: sf-reader-all [--json] <url> [url2 ...]")
            sys.exit(1)
        cmd_fetch(urls, as_json=as_json)
    else:
        print(f"❌ Unknown command: {cmd}")
        print("   Run 'sf-reader-all' with no args for help")


if __name__ == "__main__":
    main()
