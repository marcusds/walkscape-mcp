import json

import httpx
import pytest

from walkscape_mcp import wiki as wiki_mod
from walkscape_mcp.wiki import Wiki


class FakeArchive:
    def get_entry_by_path(self, path):
        raise KeyError(path)


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKSCAPE_MCP_HOME", str(tmp_path))
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "state.json").write_text(json.dumps({"tag": "v1", "published_at": "2026-09-28T02:00:00Z"}))
    w = Wiki()
    monkeypatch.setattr(w, "archive", lambda: FakeArchive())
    return w


def fake_get(pages, calls):
    def get(url, params, timeout):
        calls.append(dict(params))
        if params["action"] == "parse":
            body = {"parse": {"text": pages[params["page"]]}}
        elif "rccontinue" not in params:
            body = {"query": {"recentchanges": [
                {"title": "Summer's Reach/zh-hans", "timestamp": "2026-09-28T10:31:00Z"},
                {"title": "Summer's Reach", "timestamp": "2026-09-28T10:30:00Z"},
            ]}, "continue": {"rccontinue": "x", "continue": "-||"}}
        else:
            body = {"query": {"recentchanges": [{"title": "Summer's Reach", "timestamp": "2026-09-28T09:00:00Z"},
                                                {"title": "Termite/de", "timestamp": "2026-09-28T09:00:00Z"}]}}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    return get


def test_live_listing_skips_translations_and_continues(w, monkeypatch):
    calls = []
    monkeypatch.setattr(wiki_mod.httpx, "get", fake_get({}, calls))
    lv = w.refresh_live()
    # newest edit per page wins; translated pages are left out
    assert lv["pages"] == {"Summer's Reach": "2026-09-28T10:30:00Z"}
    # the first listing reaches back past the dump's publish time
    assert calls[0]["rcend"] == "2026-09-27T20:00:00Z"
    assert w.version() == "v1+2026-09-28T10:30:00Z"
    # the next listing only asks for changes since this one
    calls.clear()
    w.refresh_live()
    assert calls[0]["rcend"] == lv["checked"]


def test_live_page_renders_like_the_dump(w, monkeypatch):
    calls = []
    html = ('<div class="mw-parser-output"><div class="wswb-language-selector">Deutsch</div>'
            '<div id="toc">Contents</div><h2>Routes<span class="mw-editsection">[edit]</span></h2>'
            '<p>Go to <a href="/wiki/Blackrane">Blackrane</a> (522 steps).</p></div>')
    monkeypatch.setattr(wiki_mod.httpx, "get", fake_get({"Summer's Reach": html}, calls))
    w.refresh_live()
    text = w.page("summer's reach")
    assert "(source: live wiki, edited 2026-09-28T10:30:00Z)" in text
    assert text.endswith("Routes\nGo to Blackrane (522 steps).")
    # cached until the page is edited again
    n = len(calls)
    w.page("Summer's Reach")
    assert len(calls) == n


def test_new_dump_resets_live_pages(w, monkeypatch, tmp_path):
    monkeypatch.setattr(wiki_mod.httpx, "get", fake_get({}, []))
    w.refresh_live()
    (tmp_path / "wiki" / "state.json").write_text(json.dumps({"tag": "v2", "published_at": "2026-09-29T02:00:00Z"}))
    assert w.live_pages() == {}
    assert w.version() == "v2"
