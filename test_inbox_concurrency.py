"""Regression test for the inbox lost-update race + upsert.

Bug (2026-06-18): multiple sessions under the same home dir write
unified_inbox.json concurrently. The old "load-whole / append / write-whole"
save() had no locking or merge, so concurrent writers clobbered each other —
a freshly fetched article (e.g. a new WeChat URL) silently vanished. The old
URL-keyed dedup also meant re-fetching a URL never refreshed its content.

Run: <venv>/bin/python test_inbox_concurrency.py
"""

import os
import tempfile
import concurrent.futures as cf

from sf_reader_all.schema import UnifiedInbox, UnifiedContent, SourceType


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


if __name__ == "__main__":
    test_no_lost_update_under_concurrency()
    print("PASS: no lost update under concurrency (30/30)")
    test_refetch_upserts_content()
    print("PASS: re-fetch upserts content")
    test_concurrent_writers_preserve_distinct_urls()
    print("PASS: concurrent distinct URLs both persist")
    test_stale_writer_does_not_revert_a_refresh()
    print("PASS: stale writer does not revert refreshed content")
    test_clear_old_deletion_survives_merge()
    print("PASS: clear_old deletion survives disk merge")
    print("ALL PASS")
