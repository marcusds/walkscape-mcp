"""Offline wiki access via the daily ZIM dump from samuellmdev/Walkscape-Wiki-Scrapper.

Downloading the dump (one ~45 MB file per day, at most) instead of querying
wiki.walkscape.app keeps load off the wiki.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from .paths import wiki_dir

RELEASES = "https://api.github.com/repos/samuellmdev/Walkscape-Wiki-Scrapper/releases/latest"
CHECK_INTERVAL = 6 * 3600
LANG_SUFFIX = re.compile(r"/[a-z]{2}(-[a-z]{2})?$")


class Wiki:
    def __init__(self):
        self._archive = None
        self._path: Path | None = None

    @property
    def state_file(self) -> Path:
        return wiki_dir() / "state.json"

    def state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def update(self, force: bool = False) -> dict:
        """Download the newest ZIM release if we don't already have it."""
        st = self.state()
        if not force and st.get("checked_at", 0) > time.time() - CHECK_INTERVAL and Path(st.get("path", "")).exists():
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
        return st

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
        seen, out = set(), []
        for src in (
            SuggestionSearcher(a).suggest(query).getResults(0, limit * 3),
            Searcher(a).search(Query().set_query(query)).getResults(0, limit * 6),
        ):
            for p in src:
                base = LANG_SUFFIX.sub("", p)
                if base in seen or base.startswith(("_", "Devblogs", "Devupdates", "Versions/")):
                    continue
                seen.add(base)
                out.append(base.replace("_", " "))
                if len(out) >= limit:
                    return out
        return out

    def page(self, title: str, max_chars: int = 12000) -> str:
        from bs4 import BeautifulSoup

        a = self.archive()
        path = title.strip().replace(" ", "_")
        try:
            e = a.get_entry_by_path(path)
        except KeyError:
            hits = self.search(title, 1)
            if not hits:
                raise KeyError(f"No wiki page {title!r}")
            e = a.get_entry_by_path(hits[0].replace(" ", "_"))
        if e.is_redirect:
            e = e.get_redirect_entry()
        soup = BeautifulSoup(bytes(e.get_item().content).decode(), "lxml")
        for t in soup(["script", "style"]):
            t.decompose()
        # drop the language switcher and footer boilerplate
        for t in soup.select(".mw-pt-languages, .mw-pt-translate-header, footer, #mw-content-text > .noprint"):
            t.decompose()
        body = soup.find(id="mw-content-text") or soup.body
        for tr in body.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            tr.replace_with(soup.new_string(" | ".join(cells) + "\n"))
        text = body.get_text("\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        text = text.split("This article is issued from")[0].strip()
        header = f"# {e.title}\n(source: wiki dump {self.state().get('tag', '?')})\n\n"
        return header + (text[:max_chars] + "\n…[truncated]" if len(text) > max_chars else text)
