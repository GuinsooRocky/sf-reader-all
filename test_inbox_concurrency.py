"""Regression test for the inbox lost-update race + upsert.

Bug (2026-06-18): multiple sessions under the same home dir write
unified_inbox.json concurrently. The old "load-whole / append / write-whole"
save() had no locking or merge, so concurrent writers clobbered each other —
a freshly fetched article (e.g. a new WeChat URL) silently vanished. The old
URL-keyed dedup also meant re-fetching a URL never refreshed its content.

Run: <venv>/bin/python test_inbox_concurrency.py
"""

import asyncio
import builtins
import tempfile
import concurrent.futures as cf
import os
import threading
import time
from unittest.mock import patch

from sf_reader_all.reader import UniversalReader
from sf_reader_all.schema import UnifiedInbox, UnifiedContent, SourceType
from sf_reader_all.utils.storage import save_many_to_markdown


def _item(n, content=None):
    return UnifiedContent(
        source_type=SourceType.WECHAT,
        source_name="acct",
        title=f"art{n}",
        content=content if content is not None else f"c{n}",
        url=f"https://mp.weixin.qq.com/s/SLUG{n}",
    )


def test_no_lost_update_under_concurrency():
    """30 concurrent writers, each adding a distinct article, must all survive."""
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")

    def worker(n):
        inb = UnifiedInbox(fp)        # each "session" loads its own snapshot
        inb.add(_item(n))
        inb.save()

    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(worker, range(30)))

    final = UnifiedInbox(fp)
    assert len(final.items) == 30, f"lost update: expected 30, got {len(final.items)}"


def test_refetch_upserts_content():
    """Re-adding the same URL with new content refreshes it (and triggers a save)."""
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")
    inb = UnifiedInbox(fp)

    assert inb.add(_item(1, "old")) is True          # new -> added
    assert inb.add(_item(1, "old")) is False         # identical -> no change
    assert inb.add(_item(1, "fresh")) is True        # changed -> refreshed
    inb.save()

    reloaded = UnifiedInbox(fp)
    assert len(reloaded.items) == 1
    assert reloaded.items[0].content == "fresh"


def test_concurrent_writers_preserve_distinct_urls():
    """The exact field bug: two sessions saving two different URLs near-
    simultaneously must both persist (neither clobbers the other)."""
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")
    UnifiedInbox(fp).save()  # seed empty file

    def save_one(n):
        inb = UnifiedInbox(fp)
        inb.add(_item(n))
        inb.save()

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(save_one, [101, 202]))

    urls = {i.url for i in UnifiedInbox(fp).items}
    assert "https://mp.weixin.qq.com/s/SLUG101" in urls
    assert "https://mp.weixin.qq.com/s/SLUG202" in urls


def test_stale_writer_does_not_revert_a_refresh():
    """A session loaded before a refresh must not write the old body back."""
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")

    seed = UnifiedInbox(fp)
    seed.add(_item(1, "old"))
    seed.save()

    stale = UnifiedInbox(fp)
    fresh = UnifiedInbox(fp)
    fresh.add(_item(1, "fresh"))
    fresh.save()

    stale.add(_item(2, "another article"))
    stale.save()

    final = UnifiedInbox(fp)
    by_url = {item.url: item.content for item in final.items}
    assert by_url["https://mp.weixin.qq.com/s/SLUG1"] == "fresh"
    assert by_url["https://mp.weixin.qq.com/s/SLUG2"] == "another article"


def test_older_fetch_cannot_overwrite_newer_same_url():
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")

    seed_item = _item(1, "seed")
    seed_item.fetched_at = "2026-08-11T00:00:00"
    seed = UnifiedInbox(fp)
    seed.add(seed_item)
    seed.save()

    older_writer = UnifiedInbox(fp)
    newer_writer = UnifiedInbox(fp)
    older = _item(1, "older fetch")
    older.fetched_at = "2026-08-11T01:00:00"
    newer = _item(1, "newer fetch")
    newer.fetched_at = "2026-08-11T02:00:00"

    older_writer.add(older)
    newer_writer.add(newer)
    newer_writer.save()
    older_writer.save()

    final = UnifiedInbox(fp).items[0]
    assert final.content == "newer fetch"
    assert final.fetched_at == "2026-08-11T02:00:00"


def test_failed_save_remains_dirty_and_retries_identical_content():
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")

    class FailOnceInbox(UnifiedInbox):
        def __init__(self, filepath):
            self.save_calls = 0
            super().__init__(filepath)

        def save(self):
            self.save_calls += 1
            if self.save_calls == 1:
                raise OSError("transient disk failure")
            return super().save()

    inbox = FailOnceInbox(fp)
    reader = UniversalReader(inbox=inbox)
    item = _item(77, "must reach disk")

    with patch("sf_reader_all.utils.storage.save_many_to_markdown"):
        try:
            reader._persist_many([item])
        except OSError:
            pass
        else:
            raise AssertionError("first save should fail")

        assert inbox.is_dirty
        reader._persist_many([item])

    assert inbox.save_calls == 2
    assert not inbox.is_dirty
    assert UnifiedInbox(fp).items[0].content == "must reach disk"


def test_reader_persistence_is_off_loop_and_serialized():
    started = threading.Event()
    state_lock = threading.Lock()
    main_thread = threading.get_ident()

    class SlowInbox:
        def __init__(self):
            self.is_dirty = False
            self.events = []
            self.worker_threads = []
            self.active = 0
            self.peak_active = 0

        def add_batch(self, items):
            self.events.append(f"add:{items[0].title}")
            self.is_dirty = True
            return len(items)

        def save(self):
            with state_lock:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
            self.worker_threads.append(threading.get_ident())
            self.events.append("save")
            started.set()
            try:
                time.sleep(0.08)
                self.is_dirty = False
            finally:
                with state_lock:
                    self.active -= 1

    inbox = SlowInbox()
    reader = UniversalReader(inbox=inbox)

    async def persist_twice():
        first = asyncio.create_task(reader._persist_many_async([_item(801)]))
        while not started.is_set():
            await asyncio.sleep(0.002)
        assert not first.done(), "persistence blocked the event loop"
        second = asyncio.create_task(reader._persist_many_async([_item(802)]))
        await asyncio.gather(first, second)

    with patch("sf_reader_all.utils.storage.save_many_to_markdown"):
        asyncio.run(persist_twice())

    assert inbox.events == ["add:art801", "save", "add:art802", "save"]
    assert inbox.peak_active == 1
    assert all(thread_id != main_thread for thread_id in inbox.worker_threads)


def test_clear_old_deletion_survives_merge():
    """The disk merge must not resurrect items removed by clear_old()."""
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")
    inb = UnifiedInbox(fp)
    old = _item(1)
    old.fetched_at = "2000-01-01T00:00:00"
    inb.add(old)
    inb.add(_item(2))
    inb.save()

    loaded = UnifiedInbox(fp)
    loaded.clear_old(days=7)
    loaded.save()

    urls = {item.url for item in UnifiedInbox(fp).items}
    assert "https://mp.weixin.qq.com/s/SLUG1" not in urls
    assert "https://mp.weixin.qq.com/s/SLUG2" in urls


def test_batches_persist_successes_with_one_save():
    """Each partial-success batch must add and flush its successes only once."""
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "inbox.json")

    class CountingInbox(UnifiedInbox):
        def __init__(self, filepath):
            self.add_batch_calls = 0
            self.save_calls = 0
            super().__init__(filepath)

        def add_batch(self, items):
            self.add_batch_calls += 1
            return super().add_batch(items)

        def save(self):
            self.save_calls += 1
            return super().save()

    inbox = CountingInbox(fp)
    reader = UniversalReader(inbox=inbox)

    async def fake_fetch(_platform, url, *, browser_runtime=None):
        if url.endswith("/bad"):
            raise RuntimeError("expected fetch failure")
        number = url.rsplit("/", 1)[-1]
        return _item(number)

    reader._fetch = fake_fetch
    urls = [
        "https://example.com/101",
        "https://example.com/bad",
        "https://example.com/202",
    ]
    env = {"OUTPUT_DIR": "", "OBSIDIAN_VAULT": ""}
    with patch("sf_reader_all.reader.validate_url"), patch.dict(os.environ, env):
        contents = asyncio.run(reader.read_batch(urls))

        assert [item.title for item in contents] == ["art101", "art202"]
        assert inbox.add_batch_calls == 1
        assert inbox.save_calls == 1

        inbox.add_batch_calls = 0
        inbox.save_calls = 0

        async def fake_read_source(source, *, persist, browser_runtime=None):
            assert persist is False
            if source == "bad-source":
                raise RuntimeError("expected source failure")
            return _item(source)

        reader._read_source = fake_read_source
        contents = asyncio.run(reader.read_sources(["303", "bad-source", "404"]))

        assert [item.title for item in contents] == ["art303", "art404"]
        assert inbox.add_batch_calls == 1
        assert inbox.save_calls == 1

    assert len(UnifiedInbox(fp).items) == 4


def test_markdown_batch_opens_the_destination_once():
    """A batch append should cross the Markdown filesystem boundary once."""
    with tempfile.TemporaryDirectory() as directory:
        destination = os.path.join(directory, "content_hub.md")
        real_open = builtins.open
        destination_opens = 0

        def counting_open(path, *args, **kwargs):
            nonlocal destination_opens
            if os.path.abspath(os.fspath(path)) == os.path.abspath(destination):
                destination_opens += 1
            return real_open(path, *args, **kwargs)

        with (
            patch.dict(
                os.environ,
                {"OUTPUT_DIR": directory, "OBSIDIAN_VAULT": ""},
            ),
            patch("builtins.open", side_effect=counting_open),
        ):
            save_many_to_markdown([_item(901), _item(902)])

        with open(destination, encoding="utf-8") as markdown_file:
            rendered = markdown_file.read()

    assert destination_opens == 1
    assert rendered.count("## 💬 art901") == 1
    assert rendered.count("## 💬 art902") == 1


if __name__ == "__main__":
    test_no_lost_update_under_concurrency()
    print("PASS: no lost update under concurrency (30/30)")
    test_refetch_upserts_content()
    print("PASS: re-fetch upserts content")
    test_concurrent_writers_preserve_distinct_urls()
    print("PASS: concurrent distinct URLs both persist")
    test_stale_writer_does_not_revert_a_refresh()
    print("PASS: stale writer does not revert refreshed content")
    test_older_fetch_cannot_overwrite_newer_same_url()
    print("PASS: older fetch cannot overwrite newer same-URL content")
    test_failed_save_remains_dirty_and_retries_identical_content()
    print("PASS: failed save remains dirty and retries")
    test_reader_persistence_is_off_loop_and_serialized()
    print("PASS: reader persistence is off-loop and serialized")
    test_clear_old_deletion_survives_merge()
    print("PASS: clear_old deletion survives disk merge")
    test_batches_persist_successes_with_one_save()
    print("PASS: URL and mixed-source batches persist with one save each")
    test_markdown_batch_opens_the_destination_once()
    print("PASS: Markdown batch opens and appends once")
    print("ALL PASS")
