# -*- coding: utf-8 -*-
"""
Storage utilities — save content to JSON inbox and optional Markdown file.

Implements the "atomic archiving" from the tweet:
- unified_inbox.json (for AI/programmatic use)
- markdown file (for human reading, e.g. Obsidian)
"""

import json
import os
from collections.abc import Iterable
from pathlib import Path
from loguru import logger

from sf_reader_all.schema import UnifiedContent


def save_to_json(item: UnifiedContent, filepath: str = "unified_inbox.json"):
    """Append content to JSON inbox file."""
    path = Path(filepath)
    data = []

    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data = []

    data.append(item.to_dict())

    # Keep last 500 entries to prevent unbounded growth
    data = data[-500:]

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved to JSON: {path}")


def _markdown_path(filepath: str = None) -> Path | None:
    """Resolve and validate the configured Markdown destination."""
    if not filepath:
        # Priority 1: Obsidian vault
        vault_path = os.getenv("OBSIDIAN_VAULT", "")
        if vault_path:
            filepath = os.path.join(vault_path, "01-收集箱", "sf-reader-all-inbox.md")
        else:
            # Priority 2: generic output dir
            output_dir = os.getenv("OUTPUT_DIR", "")
            if not output_dir:
                return None
            filepath = os.path.join(output_dir, "content_hub.md")

    # Security: Validate filepath to prevent path traversal attacks
    # Only allow paths under explicitly configured directories or current working directory
    abs_filepath = os.path.abspath(filepath)
    
    # Get allowed directories from environment, with fallbacks
    output_dir = os.getenv("OUTPUT_DIR", "")
    vault_path = os.getenv("OBSIDIAN_VAULT", "")
    
    # Build list of allowed absolute paths (skip empty/missing env vars)
    allowed_dirs = []
    if output_dir:
        allowed_dirs.append(os.path.abspath(output_dir))
    if vault_path:
        allowed_dirs.append(os.path.abspath(vault_path))
    # Always allow current working directory
    allowed_dirs.append(os.path.abspath(os.getcwd()))
    # Always allow user's home directory
    allowed_dirs.append(os.path.abspath(os.path.expanduser("~")))
    # Always allow /tmp for temporary files
    allowed_dirs.append("/tmp")
    
    # commonpath respects path-component boundaries; a string prefix check would
    # incorrectly treat /tmp-evil as being inside /tmp.
    def is_within(directory: str) -> bool:
        try:
            return os.path.commonpath((abs_filepath, directory)) == directory
        except ValueError:
            return False

    if not any(is_within(directory) for directory in allowed_dirs):
        raise ValueError(f"Security: Refusing to write outside allowed directories: {filepath}")

    return Path(abs_filepath)


def _markdown_entry(item: UnifiedContent) -> str:
    """Render one content item without touching the filesystem."""

    emoji = {
        "telegram": "📢", "rss": "📰", "bilibili": "🎬",
        "xhs": "📕", "twitter": "🐦", "wechat": "💬",
        "youtube": "▶️", "document": "📄", "manual": "✏️",
    }.get(item.source_type.value, "📄")

    return (
        f"\n## {emoji} {item.title}\n"
        f"- Source: {item.source_name} ({item.source_type.value})\n"
        f"- URL: {item.url}\n"
        f"- Fetched: {item.fetched_at[:16]}\n\n"
        f"{item.content[:2000]}\n"
        "\n---\n"
    )


def _append_markdown(items: list[UnifiedContent], filepath: str = None) -> None:
    """Append a batch with one open, one lock, and one write call."""
    if not items:
        return
    path = _markdown_path(filepath)
    if path is None:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(_markdown_entry(item) for item in items)
    with open(path, "a", encoding="utf-8") as markdown_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            fcntl = None
        if fcntl is not None:
            fcntl.flock(markdown_file, fcntl.LOCK_EX)
        try:
            markdown_file.write(rendered)
        finally:
            if fcntl is not None:
                fcntl.flock(markdown_file, fcntl.LOCK_UN)

    logger.info(f"Saved {len(items)} item(s) to Markdown: {path}")


def save_to_markdown(item: UnifiedContent, filepath: str = None):
    """Append one item to the configured Markdown output."""
    _append_markdown([item], filepath)


def save_many_to_markdown(
    items: Iterable[UnifiedContent], filepath: str = None
) -> None:
    """Append a collection with one filesystem write boundary."""
    _append_markdown(list(items), filepath)


def save_content(item: UnifiedContent, json_path: str = None, md_path: str = None):
    """Save content to both JSON and Markdown."""
    inbox_file = json_path or os.getenv("INBOX_FILE", os.path.expanduser("~/unified_inbox.json"))
    save_to_json(item, inbox_file)
    save_to_markdown(item, md_path)
