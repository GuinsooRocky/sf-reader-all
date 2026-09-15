"""Offline checks for xhs_search: pagination, filtering, limit, error mapping.

Run: python test_xhs_search.py
"""
import json
import tempfile
from pathlib import Path
from unittest import mock

from sf_reader_all.fetchers import xhs_search


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


def note(i):
    return {"id": f"n{i}", "model_type": "note", "xsec_token": f"tok{i}",
            "note_card": {"display_title": f"title {i}", "type": "normal",
                          "user": {"nickname": "u"}, "interact_info": {"liked_count": "9"}}}


def session_file(cookies=("a1", "web_session")):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"cookies": [{"name": n, "value": "v", "domain": ".xiaohongshu.com"} for n in cookies]}, f)
    f.close()
    return f.name


def run(pages, **kw):
    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append((json.loads(data), headers))
        return pages[len(calls) - 1]

    with mock.patch.object(xhs_search.requests, "post", side_effect=fake_post):
        notes = xhs_search.search_notes("kw", session=session_file(), page_delay=(0, 0), **kw)
    return notes, calls


def expect_error(pages, fragment, session=None):
    with mock.patch.object(xhs_search.requests, "post", side_effect=pages):
        try:
            xhs_search.search_notes("kw", session=session or session_file(), page_delay=(0, 0))
        except RuntimeError as e:
            assert fragment in str(e), str(e)
            return
    raise AssertionError(f"expected error containing {fragment!r}")


# paginates until has_more is false, drops rec_query rows, dedupes
p1 = FakeResp({"success": True, "data": {"has_more": True, "items": [note(1), {"id": "q", "model_type": "rec_query"}, note(2)]}})
p2 = FakeResp({"success": True, "data": {"has_more": False, "items": [note(2), note(3)]}})
notes, calls = run([p1, p2], limit=50)
assert [n["id"] for n in notes] == ["n1", "n2", "n3"], notes
assert [c[0]["page"] for c in calls] == [1, 2]
assert calls[0][0]["search_id"] == calls[1][0]["search_id"]
assert "x-s" in calls[0][1] and "web_session=v" in calls[0][1]["cookie"]
assert notes[0]["href"] == "https://www.xiaohongshu.com/explore/n1?xsec_token=tok1&xsec_source=pc_search"

# stops once limit is reached
notes, calls = run([p1, p2], limit=1)
assert len(notes) == 1 and len(calls) == 1

# sort/type mapping
_, calls = run([p2], sort="hot", note_type="video")
assert calls[0][0]["sort"] == "popularity_descending" and calls[0][0]["note_type"] == 1

# error mapping
expect_error([FakeResp({"success": False, "code": -100, "msg": "登录已过期"})], "login xhs")
expect_error([FakeResp({}, status=461)], "CAPTCHA")
expect_error([FakeResp({"success": False, "code": 300012})], "300012")
expect_error([], "no login cookies", session=session_file(cookies=("a1",)))
expect_error([], "No saved XHS session", session=str(Path(tempfile.gettempdir()) / "nope.json"))

print("xhs_search offline checks: all passed")
