# -*- coding: utf-8 -*-
"""
Unified content schema for sf-reader-all.

Defines the standard data format for all content sources:
- Telegram channels
- RSS feeds
- Bilibili videos
- Xiaohongshu (RED) notes
- WeChat articles
- X/Twitter posts
- YouTube videos
- Manual input
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from enum import Enum
import hashlib
import json
import os
import tempfile

try:
    import fcntl  # POSIX-only; absent on Windows
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover
    _HAS_FCNTL = False


def _fetched_at_timestamp(value: str) -> float:
    """Normalize stored ISO timestamps for conflict resolution."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError):
        return float("-inf")


class SourceType(str, Enum):
    """Content source types."""
    TELEGRAM = "telegram"
    RSS = "rss"
    BILIBILI = "bilibili"
    XIAOHONGSHU = "xhs"
    TWITTER = "twitter"
    WECHAT = "wechat"
    YOUTUBE = "youtube"
    DOCUMENT = "document"
    MANUAL = "manual"


class MediaType(str, Enum):
    """Media types."""
    TEXT = "text"
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"


class Priority(str, Enum):
    """Content priority levels."""
    HOT = "hot"
    QUALITY = "quality"
    DEEP = "deep"
    NORMAL = "normal"
    LOW = "low"


@dataclass
class UnifiedContent:
    """Unified content format across all platforms."""

    # === Required ===
    source_type: SourceType
    source_name: str
    title: str
    content: str
    url: str

    # === Auto-generated ===
    id: str = ""
    fetched_at: str = ""

    # === Media ===
    media_type: MediaType = MediaType.TEXT
    media_url: Optional[str] = None

    # === Scoring ===
    score: int = 0
    priority: Priority = Priority.NORMAL
    category: str = ""
    tags: List[str] = field(default_factory=list)

    # === Processing state ===
    processed: bool = False
    digest_date: Optional[str] = None

    # === Translation ===
    title_cn: Optional[str] = None
    content_cn: Optional[str] = None

    # === Metadata ===
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = hashlib.md5(self.url.encode()).hexdigest()[:12]
        if not self.fetched_at:
            self.fetched_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        d = asdict(self)
        d['source_type'] = self.source_type.value
        d['media_type'] = self.media_type.value
        d['priority'] = self.priority.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'UnifiedContent':
        if isinstance(data.get('source_type'), str):
            data['source_type'] = SourceType(data['source_type'])
        if isinstance(data.get('media_type'), str):
            data['media_type'] = MediaType(data['media_type'])
        if isinstance(data.get('priority'), str):
            data['priority'] = Priority(data['priority'])
        known = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in known}
        return cls(**data)


# =============================================================================
# Converters: platform-specific dict → UnifiedContent
# =============================================================================

def from_telegram(msg: dict, channel_name: str, channel_username: str) -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.TELEGRAM,
        source_name=channel_name,
        title=msg.get('text', '')[:100],
        content=msg.get('text', ''),
        url=msg.get('url', f"https://t.me/{channel_username}"),
        extra={"views": msg.get('views', 0), "channel_username": channel_username},
    )


def from_rss(article: dict) -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.RSS,
        source_name=article.get('source', ''),
        title=article.get('title', ''),
        content=article.get('summary', ''),
        url=article.get('url', article.get('link', '')),
        score=article.get('score', 0),
        category=article.get('category', ''),
        title_cn=article.get('title_cn'),
        content_cn=article.get('summary_cn'),
    )


def from_bilibili(video: dict) -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.BILIBILI,
        source_name=video.get('author', ''),
        title=video.get('title', ''),
        content=video.get('description', ''),
        url=video.get('url', ''),
        media_type=MediaType.VIDEO,
        media_url=video.get('cover', ''),
        extra={
            "bvid": video.get('bvid', ''),
            "duration": video.get('duration', 0),
            "view_count": video.get('view_count', 0),
        },
    )


def from_twitter(data: dict) -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.TWITTER,
        source_name=data.get('author', ''),
        title=data.get('text', '')[:100],
        content=data.get('text', ''),
        url=data.get('url', ''),
        extra={
            "likes": data.get('likes', 0),
            "retweets": data.get('retweets', 0),
        },
    )


def from_wechat(article: dict) -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.WECHAT,
        source_name=article.get('author', ''),
        title=article.get('title', ''),
        content=article.get('content', ''),
        url=article.get('url', ''),
    )


def from_xiaohongshu(note: dict) -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.XIAOHONGSHU,
        source_name=note.get('author', ''),
        title=note.get('title', ''),
        content=note.get('content', ''),
        url=note.get('url', ''),
        media_type=MediaType.IMAGE if note.get('images') else MediaType.TEXT,
        extra={
            "likes": note.get('likes', 0),
            "collects": note.get('collects', 0),
        },
    )


def from_youtube(video: dict) -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.YOUTUBE,
        source_name=video.get('author', ''),
        title=video.get('title', ''),
        content=video.get('description', ''),
        url=video.get('url', ''),
        media_type=MediaType.VIDEO,
        extra={
            "duration": video.get('duration', ''),
            "view_count": video.get('view_count', 0),
        },
    )


def from_manual(title: str, content: str, url: str = "") -> UnifiedContent:
    return UnifiedContent(
        source_type=SourceType.MANUAL,
        source_name="manual",
        title=title,
        content=content,
        url=url or f"manual://{hashlib.md5(title.encode()).hexdigest()[:8]}",
    )


# =============================================================================
# Unified Inbox
# =============================================================================

class UnifiedInbox:
    """JSON-based content inbox with dedup."""

    def __init__(self, filepath: str = "unified_inbox.json"):
        self.filepath = filepath
        self.items: List[UnifiedContent] = []
        self._loaded_state: dict[str, dict] = {}
        self._dirty = False
        self.load()

    def load(self):
        import os
        self.items = []
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.items = [UnifiedContent.from_dict(d) for d in data]
            except (json.JSONDecodeError, IOError):
                self.items = []
        self._loaded_state = {item.id: item.to_dict() for item in self.items}
        self._dirty = False

    @property
    def is_dirty(self) -> bool:
        """Whether this in-memory inbox still has unsaved changes."""
        return self._dirty

    def save(self):
        """Persist the inbox atomically, merging with the on-disk state.

        Multiple sessions under the same home dir write this file concurrently.
        The naive "load-whole / append / write-whole" pattern loses updates: two
        processes both read v0, each appends one item, and the second writer
        clobbers the first's item. We guard against that by, under an exclusive
        file lock, re-reading the current on-disk items and merging this
        session's items on top (newest fetch wins per id), then writing to a temp
        file and os.replace()-ing it into place so readers never see a partial
        write.
        """
        lock = open(self.filepath + ".lock", "w")
        try:
            if _HAS_FCNTL:
                fcntl.flock(lock, fcntl.LOCK_EX)

            disk_items: List[UnifiedContent] = []
            if os.path.exists(self.filepath):
                try:
                    with open(self.filepath, 'r', encoding='utf-8') as f:
                        disk_items = [UnifiedContent.from_dict(d) for d in json.load(f)]
                except (json.JSONDecodeError, IOError):
                    disk_items = []

            # Apply only this session's changes to the latest disk state. A
            # session may have loaded an older copy before another writer
            # refreshed it; overlaying every item from self.items would revert
            # that newer content. Comparing against the load-time snapshot also
            # preserves deletions made by clear_old().
            current = {item.id: item for item in self.items}
            changed_ids = {
                item_id for item_id, item in current.items()
                if self._loaded_state.get(item_id) != item.to_dict()
            }
            deleted_ids = set(self._loaded_state) - set(current)

            merged = {item.id: item for item in disk_items}
            order = [item.id for item in disk_items if item.id not in deleted_ids]
            for item_id in deleted_ids:
                merged.pop(item_id, None)
            for item_id in changed_ids:
                candidate = current[item_id]
                disk_item = merged.get(item_id)
                if disk_item and _fetched_at_timestamp(
                    candidate.fetched_at
                ) < _fetched_at_timestamp(disk_item.fetched_at):
                    continue
                if item_id not in merged:
                    order.append(item_id)
                merged[item_id] = candidate
            final = [merged[i] for i in order][-500:]

            dir_ = os.path.dirname(os.path.abspath(self.filepath)) or "."
            fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump([item.to_dict() for item in final], f,
                              ensure_ascii=False, indent=2)
                os.replace(tmp, self.filepath)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

            self.items = final
            self._loaded_state = {item.id: item.to_dict() for item in final}
            self._dirty = False
        finally:
            if _HAS_FCNTL:
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def add(self, item: UnifiedContent) -> bool:
        """Upsert by id. Returns True if the inbox changed (new or refreshed),
        False if an identical item already exists. Re-fetching the same URL now
        refreshes its content instead of being silently dropped."""
        for idx, existing in enumerate(self.items):
            if existing.id == item.id:
                if existing.content == item.content and existing.title == item.title:
                    return False
                self.items[idx] = item
                self._dirty = True
                return True
        self.items.append(item)
        self._dirty = True
        return True

    def add_batch(self, items: List[UnifiedContent]) -> int:
        return sum(1 for item in items if self.add(item))

    def get_unprocessed(self) -> List[UnifiedContent]:
        return [i for i in self.items if not i.processed]

    def get_by_source(self, source_type: SourceType) -> List[UnifiedContent]:
        return [i for i in self.items if i.source_type == source_type]

    def mark_processed(self, item_id: str, digest_date: str = None):
        for item in self.items:
            if item.id == item_id:
                changed = not item.processed
                item.processed = True
                if digest_date:
                    changed = changed or item.digest_date != digest_date
                    item.digest_date = digest_date
                self._dirty = self._dirty or changed
                break

    def clear_old(self, days: int = 7):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        kept = [i for i in self.items if i.fetched_at > cutoff]
        self._dirty = self._dirty or len(kept) != len(self.items)
        self.items = kept
