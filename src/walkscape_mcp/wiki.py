"""Offline wiki access via the daily ZIM dump from samuellmdev/Walkscape-Wiki-Scrapper.

Downloading the dump (one ~45 MB file per day, at most) instead of querying
wiki.walkscape.app keeps load off the wiki. Pages edited since the dump was scraped (e.g. new locations
after a game update) are listed with one recentchanges query per check and fetched live only when read.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from calendar import timegm
from pathlib import Path

import httpx

from .paths import wiki_dir

RELEASES = "https://api.github.com/repos/samuellmdev/Walkscape-Wiki-Scrapper/releases/latest"
API = "https://wiki.walkscape.app/api.php"
CHECK_INTERVAL = 6 * 3600
LIVE_MARGIN = 6 * 3600  # the dump is scraped some time before its release is published
LANG_SUFFIX = re.compile(r"/[a-z]{2,3}(-[a-z]+)?$")
SKIP_PREFIXES = ("_", "Devblogs", "Devupdates", "Versions/")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _epoch(iso: str) -> float:
    return timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


class Wiki:
    def __init__(self):
        self._archive = None
        self._path: Path | None = None
        self._live_thread: threading.Thread | None = None

    @property
    def state_file(self) -> Path:
        return wiki_dir() / "state.json"

    def state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def update(self, force: bool = False) -> dict:
        """Download the newest ZIM release if we don't already have it, then list pages edited since (in the
        background unless forced; the first listing after a big game update takes ~30 s)."""
        st = self.state()
        if not force and st.get("checked_at", 0) > time.time() - CHECK_INTERVAL and Path(st.get("path", "")).exists():
            if self.live().get("tag") != st.get("tag"):
                self._refresh_live_quietly(st, wait=False)
            return st
        r = httpx.get(RELEASES, timeout=30, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        rel = r.json()
        asset = next(a for a in rel["assets"] if a["name"].endswith(".zim"))
        dest = wiki_dir() / f"{rel['tag_name']}.zim"
        if not dest.exists():
            tmp = dest.with_suffix(".part")
            with httpx.stream("GET", asset["browser_download_url"], follow_redirects=True, timeout=300) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in resp.iter_bytes(1 << 20):
                        f.write(chunk)
            tmp.replace(dest)
            for old in wiki_dir().glob("*.zim"):
                if old != dest:
                    old.unlink(missing_ok=True)
        st = {"tag": rel["tag_name"], "path": str(dest), "checked_at": time.time(), "published_at": rel.get("published_at")}
        self.state_file.write_text(json.dumps(st))
        if self._path != dest:
            self._archive = None
        self._refresh_live_quietly(st, wait=force)
        return st

    # ---------- pages edited since the dump ----------

    @property
    def live_file(self) -> Path:
        return wiki_dir() / "live.json"

    def live(self) -> dict:
        """{"tag": dump tag, "checked": ISO time of the last listing, "pages": {title: last edit ISO time}}"""
        try:
            return json.loads(self.live_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _refresh_live_quietly(self, st: dict, wait: bool) -> None:
        def run():
            try:
                self.refresh_live(st)
            except Exception:
                pass  # offline or the wiki is down: the dump alone still works

        if wait:
            run()
        elif not (self._live_thread and self._live_thread.is_alive()):
            self._live_thread = threading.Thread(target=run, daemon=True)
            self._live_thread.start()

    def refresh_live(self, st: dict | None = None) -> dict:
        """List English main-namespace pages edited since the dump was scraped. After the first listing per dump
        this only asks for changes since the previous check."""
        st = st or self.state()
        lv = self.live()
        if lv.get("tag") != st.get("tag"):
            shutil.rmtree(wiki_dir() / "live", ignore_errors=True)
            published = st.get("published_at")
            since = (_epoch(published) if published else time.time()) - LIVE_MARGIN
            lv = {"tag": st.get("tag"), "checked": _iso(since), "pages": {}}
        now = _iso(time.time())
        params = {"action": "query", "list": "recentchanges", "rcend": lv["checked"], "rcnamespace": "0",
                  "rctype": "edit|new", "rcprop": "title|timestamp", "rctoponly": "1", "rclimit": "500",
                  "format": "json", "formatversion": "2"}
        newer: dict[str, str] = {}
        while True:
            r = httpx.get(API, params=params, timeout=30)
            r.raise_for_status()
            d = r.json()
            for c in d["query"]["recentchanges"]:
                if not LANG_SUFFIX.search(c["title"]):
                    newer.setdefault(c["title"], c["timestamp"])
            if "continue" not in d:
                break
            params.update(d["continue"])
        lv["pages"].update(newer)
        lv["checked"] = now
        tmp = self.live_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(lv))
        tmp.replace(self.live_file)
        return lv

    def version(self) -> str | None:
        """The dump tag plus the newest live edit, for caches built from wiki pages."""
        lv = self.live()
        tag = self.state().get("tag")
        if lv.get("tag") == tag and lv.get("pages"):
            return f"{tag}+{max(lv['pages'].values())}"
        return tag

    def live_pages(self) -> dict[str, str]:
        lv = self.live()
        return (lv.get("pages") or {}) if lv.get("tag") == self.state().get("tag") else {}

    def new_pages(self) -> list[str]:
        """Pages created on the wiki since the dump was scraped (edited pages that the dump doesn't have)."""
        a = self.archive()
        out = []
        for t in self.live_pages():
            try:
                a.get_entry_by_path(t.replace(" ", "_"))
            except KeyError:
                out.append(t)
        return sorted(out)

    def _live_title(self, title: str) -> str | None:
        want = title.strip().replace("_", " ").lower()
        return next((t for t in self.live_pages() if t.lower() == want), None)

    def _live_html(self, title: str) -> str:
        edited = self.live_pages()[title]
        f = wiki_dir() / "live" / (hashlib.sha1(title.encode()).hexdigest() + ".json")
        try:
            cached = json.loads(f.read_text())
            if cached["edited"] == edited:
                return cached["html"]
        except (FileNotFoundError, ValueError, KeyError):
            pass
        r = httpx.get(API, params={"action": "parse", "page": title, "prop": "text", "redirects": "1",
                                   "format": "json", "formatversion": "2"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise KeyError(f"No wiki page {title!r}")
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps({"edited": edited, "html": d["parse"]["text"]}))
        return d["parse"]["text"]

    def archive(self):
        from libzim.reader import Archive

        st = self.state()
        path = Path(st.get("path", ""))
        if not path.exists():
            st = self.update(force=True)
            path = Path(st["path"])
        if self._archive is None or self._path != path:
            self._archive = Archive(str(path))
            self._path = path
        return self._archive

    def search(self, query: str, limit: int = 10) -> list[str]:
        from libzim.search import Query, Searcher
        from libzim.suggestion import SuggestionSearcher

        a = self.archive()
        words = query.lower().split()
        live = [t for t in self.live_pages() if not t.startswith(SKIP_PREFIXES) and all(w in t.lower() for w in words)]
        live.sort(key=lambda t: (t.lower() != query.lower(), len(t)))
        seen, out = {t.replace(" ", "_") for t in live[:limit]}, live[:limit]
        if len(out) >= limit:
            return out
        for src in (
            SuggestionSearcher(a).suggest(query).getResults(0, limit * 3),
            Searcher(a).search(Query().set_query(query)).getResults(0, limit * 6),
        ):
            for p in src:
                base = LANG_SUFFIX.sub("", p)
                if base in seen or base.startswith(SKIP_PREFIXES):
                    continue
                seen.add(base)
                out.append(base.replace("_", " "))
                if len(out) >= limit:
                    return out
        return out

    def page(self, title: str, max_chars: int = 12000) -> str:
        from bs4 import BeautifulSoup

        a = self.archive()
        live = self._live_title(title)
        if not live:
            path = title.strip().replace(" ", "_")
            try:
                e = a.get_entry_by_path(path)
            except KeyError:
                hits = self.search(title, 1)
                if not hits:
                    raise KeyError(f"No wiki page {title!r}") from None
                live = self._live_title(hits[0])
                if not live:
                    e = a.get_entry_by_path(hits[0].replace(" ", "_"))
        if live:
            name, html = live, self._live_html(live)
            source = f"live wiki, edited {self.live_pages()[live]}"
        else:
            if e.is_redirect:
                e = e.get_redirect_entry()
            name, html = e.title, bytes(e.get_item().content).decode()
            source = f"wiki dump {self.state().get('tag', '?')}"
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style"]):
            t.decompose()
        # drop the language switcher and footer boilerplate
        for t in soup.select(".mw-pt-languages, .mw-pt-translate-header, .wswb-language-selector, footer, "
                             "#mw-content-text > .noprint, .mw-parser-output > .noprint"):
            t.decompose()
        if live:  # match the dump's rendering: no links, table of contents or edit links
            for t in soup.select("#toc, .toc, .mw-editsection"):
                t.decompose()
            for t in soup.find_all("a"):
                t.unwrap()
            soup.smooth()
        body = soup.find(id="mw-content-text") or soup.body
        for tr in body.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            tr.replace_with(soup.new_string(" | ".join(cells) + "\n"))
        text = body.get_text("\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        text = text.split("This article is issued from")[0].strip()
        header = f"# {name}\n(source: {source})\n\n"
        return header + (text[:max_chars] + "\n…[truncated]" if len(text) > max_chars else text)
